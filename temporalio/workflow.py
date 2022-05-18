"""Utilities that can decorate or be called inside workflows."""

from __future__ import annotations

import asyncio
import inspect
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import IntEnum
from functools import partial
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Generic,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
    overload,
)

from typing_extensions import Literal, TypedDict

import temporalio.bridge.proto.child_workflow
import temporalio.bridge.proto.workflow_commands
import temporalio.common
import temporalio.exceptions

WorkflowClass = TypeVar("WorkflowClass", bound=Type)
ChildWorkflowClass = TypeVar("ChildWorkflowClass")
LocalParamType = TypeVar("LocalParamType")
ActivityReturnType = TypeVar("ActivityReturnType")
WorkflowReturnType = TypeVar("WorkflowReturnType")
T = TypeVar("T")


@overload
def defn(cls: WorkflowClass) -> WorkflowClass:
    ...


@overload
def defn(*, name: str) -> Callable[[WorkflowClass], WorkflowClass]:
    ...


def defn(cls: Optional[WorkflowClass] = None, *, name: Optional[str] = None):
    """Decorator for workflow classes.

    This must be set on any registered workflow class (it is ignored if on a
    base class).

    Args:
        cls: The class to decorate.
        name: Name to use for the workflow. Defaults to class ``__name__``.
    """

    def with_name(name: str, cls: WorkflowClass) -> WorkflowClass:
        # This performs validation
        _Definition._apply_to_class(cls, name)
        return cls

    # If name option is available, return decorator function
    if name is not None:
        return partial(with_name, name)
    if cls is None:
        raise RuntimeError("Cannot create defn without class or name")
    # Otherwise just run decorator function
    return with_name(cls.__name__, cls)


WorkflowRunFunc = TypeVar("WorkflowRunFunc", bound=Callable[..., Awaitable[Any]])


def run(fn: WorkflowRunFunc) -> WorkflowRunFunc:
    """Decorator for the workflow run method.

    This must be set on one and only one async method defined on the same class
    as ``@workflow.defn``. This can be defined on a base class method but must
    then be explicitly overridden and defined on the workflow class.

    Run methods can only have positional parameters. Best practice is to only
    take a single object/dataclass argument that can accept more fields later if
    needed.

    Args:
        fn: The function to decorate.
    """
    if not inspect.iscoroutinefunction(fn):
        raise ValueError("Workflow run method must be an async function")
    # Disallow local classes
    if "<locals>" in fn.__qualname__:
        raise ValueError(
            "Local classes unsupported, @workflow.run cannot be on a local class"
        )
    setattr(fn, "__temporal_workflow_run", True)
    return fn


WorkflowSignalFunc = TypeVar(
    "WorkflowSignalFunc", bound=Callable[..., Union[None, Awaitable[None]]]
)


@overload
def signal(fn: WorkflowSignalFunc) -> WorkflowSignalFunc:
    ...


@overload
def signal(*, name: str) -> Callable[[WorkflowSignalFunc], WorkflowSignalFunc]:
    ...


@overload
def signal(
    *, dynamic: Literal[True]
) -> Callable[[WorkflowSignalFunc], WorkflowSignalFunc]:
    ...


def signal(
    fn: Optional[WorkflowSignalFunc] = None,
    *,
    name: Optional[str] = None,
    dynamic: Optional[bool] = False,
):
    """Decorator for a workflow signal method.

    This is set on any async or non-async method that expects to receive a
    signal. If a function overrides one with this decorator, it too must be
    decorated.

    Signal methods can only have positional parameters. Best practice for
    non-dynamic signal methods is to only take a single object/dataclass
    argument that can accept more fields later if needed. Return values from
    signal methods are ignored.

    Args:
        fn: The function to decorate.
        name: Signal name. Defaults to method ``__name__``. Cannot be present
            when ``dynamic`` is present.
        dynamic: If true, this handles all signals not otherwise handled. The
            parameters of the method must be self, a string name, and a
            ``*args`` positional varargs. Cannot be present when ``name`` is
            present.
    """

    def with_name(name: Optional[str], fn: WorkflowSignalFunc) -> WorkflowSignalFunc:
        if not name:
            _assert_dynamic_signature(fn)
        # TODO(cretz): Validate type attributes?
        setattr(fn, "__temporal_signal_definition", _SignalDefinition(name=name, fn=fn))
        return fn

    if name is not None or dynamic:
        if name is not None and dynamic:
            raise RuntimeError("Cannot provide name and dynamic boolean")
        return partial(with_name, name)
    if fn is None:
        raise RuntimeError("Cannot create signal without function or name or dynamic")
    return with_name(fn.__name__, fn)


WorkflowQueryFunc = TypeVar("WorkflowQueryFunc", bound=Callable[..., Any])


@overload
def query(fn: WorkflowQueryFunc) -> WorkflowQueryFunc:
    ...


@overload
def query(*, name: str) -> Callable[[WorkflowQueryFunc], WorkflowQueryFunc]:
    ...


@overload
def query(
    *, dynamic: Literal[True]
) -> Callable[[WorkflowQueryFunc], WorkflowQueryFunc]:
    ...


def query(
    fn: Optional[WorkflowQueryFunc] = None,
    *,
    name: Optional[str] = None,
    dynamic: Optional[bool] = False,
):
    """Decorator for a workflow query method.

    This is set on any async or non-async method that expects to handle a
    query. If a function overrides one with this decorator, it too must be
    decorated.

    Query methods can only have positional parameters. Best practice for
    non-dynamic query methods is to only take a single object/dataclass
    argument that can accept more fields later if needed. The return value is
    the resulting query value. Query methods must not mutate any workflow state.

    Args:
        fn: The function to decorate.
        name: Query name. Defaults to method ``__name__``. Cannot be present
            when ``dynamic`` is present.
        dynamic: If true, this handles all queries not otherwise handled. The
            parameters of the method must be self, a string name, and a
            ``*args`` positional varargs. Cannot be present when ``name`` is
            present.
    """

    def with_name(name: Optional[str], fn: WorkflowQueryFunc) -> WorkflowQueryFunc:
        if not name:
            _assert_dynamic_signature(fn)
        # TODO(cretz): Validate type attributes?
        setattr(fn, "__temporal_query_definition", _QueryDefinition(name=name, fn=fn))
        return fn

    if name is not None or dynamic:
        if name is not None and dynamic:
            raise RuntimeError("Cannot provide name and dynamic boolean")
        return partial(with_name, name)
    if fn is None:
        raise RuntimeError("Cannot create query without function or name or dynamic")
    return with_name(fn.__name__, fn)


def _assert_dynamic_signature(fn: Callable) -> None:
    # If dynamic, must have three args: self, name, and varargs
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if (
        len(params) != 3
        or params[1].kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD
        or params[2].kind is not inspect.Parameter.VAR_POSITIONAL
    ):
        raise RuntimeError(
            "Dynamic handler must have 3 arguments: self, name, and var args"
        )


@dataclass(frozen=True)
class Info:
    """Information about the running workflow.

    Retrieved inside a workflow via :py:func:`info`.
    """

    attempt: int
    cron_schedule: Optional[str]
    execution_timeout: Optional[timedelta]
    namespace: str
    run_id: str
    run_timeout: Optional[timedelta]
    start_time: datetime
    task_queue: str
    task_timeout: timedelta
    workflow_id: str
    workflow_type: str

    # TODO(cretz): continued_run_id
    # TODO(cretz): memo
    # TODO(cretz): parent_namespace
    # TODO(cretz): parent_run_id
    # TODO(cretz): parent_workflow_id
    # TODO(cretz): retry_policy
    # TODO(cretz): search_attributes

    def _logger_details(self) -> Mapping[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "workflow_type": self.workflow_type,
            # TODO(cretz): more
        }


class _Runtime(ABC):
    @staticmethod
    def current() -> _Runtime:
        loop = _Runtime.maybe_current()
        if not loop:
            raise RuntimeError("Not in workflow event loop")
        return loop

    @staticmethod
    def maybe_current() -> Optional[_Runtime]:
        return getattr(asyncio.get_running_loop(), "__temporal_workflow_runtime", None)

    @staticmethod
    def set_on_loop(
        loop: asyncio.AbstractEventLoop, runtime: Optional[_Runtime]
    ) -> None:
        if runtime:
            setattr(loop, "__temporal_workflow_runtime", runtime)
        elif hasattr(loop, "__temporal_workflow_runtime"):
            delattr(loop, "__temporal_workflow_runtime")

    def __init__(self) -> None:
        super().__init__()
        self._logger_details: Optional[Mapping[str, Any]] = None

    @property
    def logger_details(self) -> Mapping[str, Any]:
        if self._logger_details is None:
            self._logger_details = self.workflow_info()._logger_details()
        return self._logger_details

    @abstractmethod
    def workflow_info(self) -> Info:
        ...

    @abstractmethod
    def workflow_now(self) -> datetime:
        ...

    @abstractmethod
    def workflow_start_activity(
        self,
        activity: Any,
        *args: Any,
        activity_id: Optional[str],
        task_queue: Optional[str],
        schedule_to_close_timeout: Optional[timedelta],
        schedule_to_start_timeout: Optional[timedelta],
        start_to_close_timeout: Optional[timedelta],
        heartbeat_timeout: Optional[timedelta],
        retry_policy: Optional[temporalio.common.RetryPolicy],
        cancellation_type: ActivityCancellationType,
    ) -> ActivityHandle[Any]:
        ...

    @abstractmethod
    async def workflow_start_child_workflow(
        self,
        workflow: Any,
        *args: Any,
        id: str,
        task_queue: Optional[str],
        namespace: Optional[str],
        cancellation_type: ChildWorkflowCancellationType,
        parent_close_policy: ParentClosePolicy,
        execution_timeout: Optional[timedelta],
        run_timeout: Optional[timedelta],
        task_timeout: Optional[timedelta],
        id_reuse_policy: temporalio.common.WorkflowIDReusePolicy,
        retry_policy: Optional[temporalio.common.RetryPolicy],
        cron_schedule: str,
        memo: Optional[Mapping[str, Any]],
        search_attributes: Optional[Mapping[str, Any]],
    ) -> ChildWorkflowHandle[Any, Any]:
        ...

    @abstractmethod
    def workflow_start_local_activity(
        self,
        activity: Any,
        *args: Any,
        activity_id: Optional[str],
        schedule_to_close_timeout: Optional[timedelta],
        schedule_to_start_timeout: Optional[timedelta],
        start_to_close_timeout: Optional[timedelta],
        retry_policy: Optional[temporalio.common.RetryPolicy],
        local_retry_threshold: Optional[timedelta],
        cancellation_type: ActivityCancellationType,
    ) -> ActivityHandle[Any]:
        ...

    @abstractmethod
    async def workflow_wait_condition(
        self, fn: Callable[[], bool], *, timeout: Optional[float] = None
    ) -> None:
        ...


def info() -> Info:
    """Current workflow's info.

    Returns:
        Info for the currently running workflow.
    """
    return _Runtime.current().workflow_info()


def now() -> datetime:
    """Current time from the workflow perspective.

    Returns:
        UTC datetime for the current workflow time
    """
    return _Runtime.current().workflow_now()


async def wait_condition(
    fn: Callable[[], bool], *, timeout: Optional[float] = None
) -> None:
    """Wait on a callback to become true.

    This function returns when the callback returns true (invoked each loop
    iteration) or the timeout has been reached.

    Args:
        fn: Non-async callback that accepts no parameters and returns a boolean.
        timeout: Optional number of seconds to wait until throwing
            :py:class:`asyncio.TimeoutError`.
    """
    await _Runtime.current().workflow_wait_condition(fn, timeout=timeout)


class LoggerAdapter(logging.LoggerAdapter):
    """Adapter that adds details to the log about the running workflow.

    Attributes:
        workflow_info_on_message: Boolean for whether a string representation of
            a dict of some workflow info will be appended to each message.
            Default is True.
        workflow_info_on_extra: Boolean for whether a ``workflow_info`` value
            will be added to the ``extra`` dictionary, making it present on the
            ``LogRecord.__dict__`` for use by others.
    """

    def __init__(
        self, logger: logging.Logger, extra: Optional[Mapping[str, Any]]
    ) -> None:
        """Create the logger adapter."""
        super().__init__(logger, extra or {})
        self.workflow_info_on_message = True
        self.workflow_info_on_extra = True

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> Tuple[Any, MutableMapping[str, Any]]:
        """Override to add workflow details."""
        msg, kwargs = super().process(msg, kwargs)
        if self.workflow_info_on_message or self.workflow_info_on_extra:
            runtime = _Runtime.maybe_current()
            if runtime:
                if self.workflow_info_on_message:
                    msg = f"{msg} ({runtime.logger_details})"
                if self.workflow_info_on_extra:
                    # Extra can be absent or None, this handles both
                    extra = kwargs.get("extra", None) or {}
                    extra["workflow_info"] = runtime.workflow_info()
                    kwargs["extra"] = extra
        return (msg, kwargs)

    @property
    def base_logger(self) -> logging.Logger:
        """Underlying logger usable for actions such as adding
        handlers/formatters.
        """
        return self.logger


#: Logger that will have contextual workflow details embedded.
logger = LoggerAdapter(logging.getLogger(__name__), None)


@dataclass(frozen=True)
class _Definition:
    name: str
    cls: Type
    run_fn: Callable[..., Awaitable]
    signals: Mapping[Optional[str], _SignalDefinition]
    queries: Mapping[Optional[str], _QueryDefinition]

    @staticmethod
    def from_class(cls: Type) -> Optional[_Definition]:
        return getattr(cls, "__temporal_workflow_definition", None)

    @staticmethod
    def must_from_class(cls: Type) -> _Definition:
        ret = _Definition.from_class(cls)
        if ret:
            return ret
        cls_name = getattr(cls, "__name__", "<unknown>")
        raise ValueError(
            f"Workflow {cls_name} missing attributes, was it decorated with @workflow.defn?"
        )

    @staticmethod
    def from_run_fn(fn: Callable[..., Awaitable[Any]]) -> Optional[_Definition]:
        return getattr(fn, "__temporal_workflow_definition", None)

    @staticmethod
    def must_from_run_fn(fn: Callable[..., Awaitable[Any]]) -> _Definition:
        ret = _Definition.from_run_fn(fn)
        if ret:
            return ret
        fn_name = getattr(fn, "__qualname__", "<unknown>")
        raise ValueError(
            f"Function {fn_name} missing attributes, was it decorated with @workflow.run and was its class decorated with @workflow.defn?"
        )

    @staticmethod
    def _apply_to_class(cls: Type, workflow_name: str) -> None:
        if hasattr(cls, "__temporal_workflow_definition"):
            raise ValueError("Class already contains workflow definition")
        issues: List[str] = []

        # Collect run fn and all signal/query fns
        members = inspect.getmembers(cls)
        run_fn: Optional[Callable[..., Awaitable[Any]]] = None
        seen_run_attr = False
        signals: Dict[Optional[str], _SignalDefinition] = {}
        queries: Dict[Optional[str], _QueryDefinition] = {}
        for name, member in members:
            if hasattr(member, "__temporal_workflow_run"):
                seen_run_attr = True
                if not _is_unbound_method_on_cls(member, cls):
                    issues.append(
                        f"@workflow.run method {name} must be defined on {cls.__qualname__}"
                    )
                elif run_fn is not None:
                    issues.append(
                        f"Multiple @workflow.run methods found (at least on {name} and {run_fn.__name__})"
                    )
                else:
                    # We can guarantee the @workflow.run decorator did
                    # validation of the function itself
                    run_fn = member
            elif hasattr(member, "__temporal_signal_definition"):
                signal_defn = cast(
                    _SignalDefinition, getattr(member, "__temporal_signal_definition")
                )
                if signal_defn.name in signals:
                    defn_name = signal_defn.name or "<dynamic>"
                    # TODO(cretz): Remove cast when https://github.com/python/mypy/issues/5485 fixed
                    other_fn = cast(Callable, signals[signal_defn.name].fn)
                    issues.append(
                        f"Multiple signal methods found for {defn_name} "
                        f"(at least on {name} and {other_fn.__name__})"
                    )
                else:
                    signals[signal_defn.name] = signal_defn
            elif hasattr(member, "__temporal_query_definition"):
                query_defn = cast(
                    _QueryDefinition, getattr(member, "__temporal_query_definition")
                )
                if query_defn.name in queries:
                    defn_name = query_defn.name or "<dynamic>"
                    issues.append(
                        f"Multiple query methods found for {defn_name} "
                        f"(at least on {name} and {queries[query_defn.name].fn.__name__})"
                    )
                else:
                    queries[query_defn.name] = query_defn

        # Check base classes haven't defined things with different decorators
        for base_cls in inspect.getmro(cls)[1:]:
            for _, base_member in inspect.getmembers(base_cls):
                # We only care about methods defined on this class
                if not inspect.isfunction(base_member) or not _is_unbound_method_on_cls(
                    base_member, base_cls
                ):
                    continue
                if hasattr(base_member, "__temporal_workflow_run"):
                    seen_run_attr = True
                    if not run_fn or base_member.__name__ != run_fn.__name__:
                        issues.append(
                            f"@workflow.run defined on {base_member.__qualname__} but not on the override"
                        )
                elif hasattr(base_member, "__temporal_signal_definition"):
                    signal_defn = cast(
                        _SignalDefinition,
                        getattr(base_member, "__temporal_signal_definition"),
                    )
                    if signal_defn.name not in signals:
                        issues.append(
                            f"@workflow.signal defined on {base_member.__qualname__} but not on the override"
                        )
                elif hasattr(base_member, "__temporal_query_definition"):
                    query_defn = cast(
                        _QueryDefinition,
                        getattr(base_member, "__temporal_query_definition"),
                    )
                    if query_defn.name not in queries:
                        issues.append(
                            f"@workflow.query defined on {base_member.__qualname__} but not on the override"
                        )

        if not seen_run_attr:
            issues.append("Missing @workflow.run method")
        if len(issues) == 1:
            raise ValueError(f"Invalid workflow class: {issues[0]}")
        elif issues:
            raise ValueError(
                f"Invalid workflow class for {len(issues)} reasons: {', '.join(issues)}"
            )

        assert run_fn
        defn = _Definition(
            name=workflow_name, cls=cls, run_fn=run_fn, signals=signals, queries=queries
        )
        setattr(cls, "__temporal_workflow_definition", defn)
        setattr(run_fn, "__temporal_workflow_definition", defn)


@dataclass(frozen=True)
class _SignalDefinition:
    # None if dynamic
    name: Optional[str]
    fn: Callable[..., Union[None, Awaitable[None]]]

    @staticmethod
    def from_fn(fn: Callable) -> Optional[_SignalDefinition]:
        return getattr(fn, "__temporal_signal_definition", None)


@dataclass(frozen=True)
class _QueryDefinition:
    # None if dynamic
    name: Optional[str]
    fn: Callable[..., Any]

    @staticmethod
    def from_fn(fn: Callable) -> Optional[_QueryDefinition]:
        return getattr(fn, "__temporal_query_definition", None)


class ActivityHandle(asyncio.Task[ActivityReturnType]):
    """Handle returned from :py:func:`start_activity` and
    :py:func:`start_local_activity`.

    This extends :py:class:`asyncio.Task` and supports all task features.
    """

    pass


class ActivityCancellationType(IntEnum):
    """How an activity cancellation should be handled."""

    TRY_CANCEL = int(
        temporalio.bridge.proto.workflow_commands.ActivityCancellationType.TRY_CANCEL
    )
    WAIT_CANCELLATION_COMPLETED = int(
        temporalio.bridge.proto.workflow_commands.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED
    )
    ABANDON = int(
        temporalio.bridge.proto.workflow_commands.ActivityCancellationType.ABANDON
    )


class ActivityConfig(TypedDict, total=False):
    """TypedDict of config that can be used for :py:func:`start_activity` and
    :py:func:`execute_activity`.
    """

    activity_id: Optional[str]
    task_queue: Optional[str]
    schedule_to_close_timeout: Optional[timedelta]
    schedule_to_start_timeout: Optional[timedelta]
    start_to_close_timeout: Optional[timedelta]
    heartbeat_timeout: Optional[timedelta]
    retry_policy: Optional[temporalio.common.RetryPolicy]
    cancellation_type: ActivityCancellationType


# Overload for async no-param activity
@overload
def start_activity(
    activity: Callable[[], Awaitable[ActivityReturnType]],
    /,
    *,
    activity_id: Optional[str] = None,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
) -> ActivityHandle[ActivityReturnType]:
    ...


# Overload for sync no-param activity
@overload
def start_activity(
    activity: Callable[[], ActivityReturnType],
    /,
    *,
    activity_id: Optional[str] = None,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
) -> ActivityHandle[ActivityReturnType]:
    ...


# Overload for async single-param activity
@overload
def start_activity(
    activity: Callable[[LocalParamType], Awaitable[ActivityReturnType]],
    arg: LocalParamType,
    /,
    *,
    activity_id: Optional[str] = None,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
) -> ActivityHandle[ActivityReturnType]:
    ...


# Overload for sync single-param activity
@overload
def start_activity(
    activity: Callable[[LocalParamType], ActivityReturnType],
    arg: LocalParamType,
    /,
    *,
    activity_id: Optional[str] = None,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
) -> ActivityHandle[ActivityReturnType]:
    ...


# Overload for string activity
@overload
def start_activity(
    activity: str,
    *args: Any,
    activity_id: Optional[str] = None,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
) -> ActivityHandle[Any]:
    ...


def start_activity(
    activity: Any,
    *args: Any,
    activity_id: Optional[str] = None,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
) -> ActivityHandle[Any]:
    """Start an activity and return its handle.

    At least one of ``schedule_to_close_timeout`` or ``start_to_close_timeout``
    must be present.

    Args:
        activity: Activity name or function reference.
        args: Arguments for the activity if any.
        activity_id: Optional unique identifier for the activity.
        task_queue: Task queue to run the activity on. Defaults to the current
            workflow's task queue.
        schedule_to_close_timeout: Max amount of time the activity can take from
            first being scheduled to being completed before it times out. This
            is inclusive of all retries.
        schedule_to_start_timeout: Max amount of time the activity can take to
            be started from first being scheduled.
        start_to_close_timeout: Max amount of time a single activity run can
            take from when it starts to when it completes. This is per retry.
        heartbeat_timeout: How frequently an activity must invoke heartbeat
            while running before it is considered timed out.
        retry_policy: How an activity is retried on failure. If unset, a
            server-defined default is used. Set maximum attempts to 1 to disable
            retries.
        cancellation_type: How the activity is treated when it is cancelled from
            the workflow.

    Returns:
        An activity handle to the activity which is an async task.
    """
    return _Runtime.current().workflow_start_activity(
        activity,
        *args,
        activity_id=activity_id,
        task_queue=task_queue,
        schedule_to_close_timeout=schedule_to_close_timeout,
        schedule_to_start_timeout=schedule_to_start_timeout,
        start_to_close_timeout=start_to_close_timeout,
        heartbeat_timeout=heartbeat_timeout,
        retry_policy=retry_policy,
        cancellation_type=cancellation_type,
    )


# Overload for async no-param activity
@overload
async def execute_activity(
    activity: Callable[[], Awaitable[ActivityReturnType]],
    /,
    *,
    activity_id: Optional[str] = None,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
) -> ActivityReturnType:
    ...


# Overload for sync no-param activity
@overload
async def execute_activity(
    activity: Callable[[], ActivityReturnType],
    /,
    *,
    activity_id: Optional[str] = None,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
) -> ActivityReturnType:
    ...


# Overload for async single-param activity
@overload
async def execute_activity(
    activity: Callable[[LocalParamType], Awaitable[ActivityReturnType]],
    arg: LocalParamType,
    /,
    *,
    activity_id: Optional[str] = None,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
) -> ActivityReturnType:
    ...


# Overload for sync single-param activity
@overload
async def execute_activity(
    activity: Callable[[LocalParamType], ActivityReturnType],
    arg: LocalParamType,
    /,
    *,
    activity_id: Optional[str] = None,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
) -> ActivityReturnType:
    ...


# Overload for string activity
@overload
async def execute_activity(
    activity: str,
    *args: Any,
    activity_id: Optional[str] = None,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
) -> Any:
    ...


async def execute_activity(
    activity: Any,
    *args: Any,
    activity_id: Optional[str] = None,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
) -> Any:
    """Start a local workflow and wait for completion.

    This is a shortcut for ``await`` :py:meth:`start_activity`.
    """
    # We call the runtime directly instead of top-level start_activity to ensure
    # we don't miss new parameters
    return await _Runtime.current().workflow_start_activity(
        activity,
        *args,
        activity_id=activity_id,
        task_queue=task_queue,
        schedule_to_close_timeout=schedule_to_close_timeout,
        schedule_to_start_timeout=schedule_to_start_timeout,
        start_to_close_timeout=start_to_close_timeout,
        heartbeat_timeout=heartbeat_timeout,
        retry_policy=retry_policy,
        cancellation_type=cancellation_type,
    )


class LocalActivityConfig(TypedDict, total=False):
    """TypedDict of config that can be used for :py:func:`start_local_activity`
    and :py:func:`execute_local_activity`.
    """

    activity_id: Optional[str]
    schedule_to_close_timeout: Optional[timedelta]
    schedule_to_start_timeout: Optional[timedelta]
    start_to_close_timeout: Optional[timedelta]
    retry_policy: Optional[temporalio.common.RetryPolicy]
    local_retry_threshold: Optional[timedelta]
    cancellation_type: ActivityCancellationType


# Overload for async no-param activity
@overload
def start_local_activity(
    activity: Callable[[], Awaitable[ActivityReturnType]],
    /,
    *,
    activity_id: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
) -> ActivityHandle[ActivityReturnType]:
    ...


# Overload for sync no-param activity
@overload
def start_local_activity(
    activity: Callable[[], ActivityReturnType],
    /,
    *,
    activity_id: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
) -> ActivityHandle[ActivityReturnType]:
    ...


# Overload for async single-param activity
@overload
def start_local_activity(
    activity: Callable[[LocalParamType], Awaitable[ActivityReturnType]],
    arg: LocalParamType,
    /,
    *,
    activity_id: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
) -> ActivityHandle[ActivityReturnType]:
    ...


# Overload for sync single-param activity
@overload
def start_local_activity(
    activity: Callable[[LocalParamType], ActivityReturnType],
    arg: LocalParamType,
    /,
    *,
    activity_id: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
) -> ActivityHandle[ActivityReturnType]:
    ...


# Overload for string activity
@overload
def start_local_activity(
    activity: str,
    *args: Any,
    activity_id: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
) -> ActivityHandle[Any]:
    ...


def start_local_activity(
    activity: Any,
    *args: Any,
    activity_id: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
) -> ActivityHandle[Any]:
    """Start a local activity and return its handle.

    At least one of ``schedule_to_close_timeout`` or ``start_to_close_timeout``
    must be present.

    Args:
        activity: Activity name or function reference.
        args: Arguments for the activity if any.
        activity_id: Optional unique identifier for the activity.
        schedule_to_close_timeout: Max amount of time the activity can take from
            first being scheduled to being completed before it times out. This
            is inclusive of all retries.
        schedule_to_start_timeout: Max amount of time the activity can take to
            be started from first being scheduled.
        start_to_close_timeout: Max amount of time a single activity run can
            take from when it starts to when it completes. This is per retry.
        retry_policy: How an activity is retried on failure. If unset, an
            SDK-defined default is used. Set maximum attempts to 1 to disable
            retries.
        cancellation_type: How the activity is treated when it is cancelled from
            the workflow.

    Returns:
        An activity handle to the activity which is an async task.
    """
    return _Runtime.current().workflow_start_local_activity(
        activity,
        *args,
        activity_id=activity_id,
        schedule_to_close_timeout=schedule_to_close_timeout,
        schedule_to_start_timeout=schedule_to_start_timeout,
        start_to_close_timeout=start_to_close_timeout,
        retry_policy=retry_policy,
        local_retry_threshold=local_retry_threshold,
        cancellation_type=cancellation_type,
    )


# Overload for async no-param activity
@overload
async def execute_local_activity(
    activity: Callable[[], Awaitable[ActivityReturnType]],
    /,
    *,
    activity_id: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
) -> ActivityReturnType:
    ...


# Overload for sync no-param activity
@overload
async def execute_local_activity(
    activity: Callable[[], ActivityReturnType],
    /,
    *,
    activity_id: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
) -> ActivityReturnType:
    ...


# Overload for async single-param activity
@overload
async def execute_local_activity(
    activity: Callable[[LocalParamType], Awaitable[ActivityReturnType]],
    arg: LocalParamType,
    /,
    *,
    activity_id: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
) -> ActivityReturnType:
    ...


# Overload for sync single-param activity
@overload
async def execute_local_activity(
    activity: Callable[[LocalParamType], ActivityReturnType],
    arg: LocalParamType,
    /,
    *,
    activity_id: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
) -> ActivityReturnType:
    ...


# Overload for string activity
@overload
async def execute_local_activity(
    activity: str,
    *args: Any,
    activity_id: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
) -> Any:
    ...


async def execute_local_activity(
    activity: Any,
    *args: Any,
    activity_id: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
) -> Any:
    """Start a local workflow and wait for completion.

    This is a shortcut for ``await`` :py:meth:`start_local_activity`.
    """
    # We call the runtime directly instead of top-level start_local_activity to
    # ensure we don't miss new parameters
    return await _Runtime.current().workflow_start_local_activity(
        activity,
        *args,
        activity_id=activity_id,
        schedule_to_close_timeout=schedule_to_close_timeout,
        schedule_to_start_timeout=schedule_to_start_timeout,
        start_to_close_timeout=start_to_close_timeout,
        retry_policy=retry_policy,
        local_retry_threshold=local_retry_threshold,
        cancellation_type=cancellation_type,
    )


class ChildWorkflowHandle(
    asyncio.Task[WorkflowReturnType], Generic[ChildWorkflowClass, WorkflowReturnType]
):
    """Handle for interacting with a child workflow.

    This is usually created via :py:meth:`Client.get_workflow_handle` or
    returned from :py:meth:`Client.start_workflow`.

    This extends :py:class:`asyncio.Task` and supports all task features.
    """

    @property
    def id(self) -> str:
        """ID for the workflow."""
        raise NotImplementedError

    @property
    def original_run_id(self) -> Optional[str]:
        """Run ID for the workflow."""
        raise NotImplementedError

    @overload
    async def signal(
        self,
        signal: Callable[[ChildWorkflowClass], Union[Awaitable[None], None]],
        /,
    ) -> None:
        ...

    @overload
    async def signal(
        self,
        signal: Callable[
            [ChildWorkflowClass, LocalParamType], Union[Awaitable[None], None]
        ],
        arg: LocalParamType,
        /,
    ) -> None:
        ...

    @overload
    async def signal(self, signal: str, *args: Any) -> None:
        ...

    async def signal(self, signal: Union[str, Callable], *args: Any) -> None:
        """Signal this child workflow.

        Args:
            signal: Name or method reference for the signal.
            args: Arguments for the signal if any.

        """
        raise NotImplementedError


class ChildWorkflowCancellationType(IntEnum):
    """How a child workflow cancellation should be handled."""

    ABANDON = int(
        temporalio.bridge.proto.child_workflow.ChildWorkflowCancellationType.ABANDON
    )
    TRY_CANCEL = int(
        temporalio.bridge.proto.child_workflow.ChildWorkflowCancellationType.TRY_CANCEL
    )
    WAIT_CANCELLATION_COMPLETED = int(
        temporalio.bridge.proto.child_workflow.ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED
    )
    WAIT_CANCELLATION_REQUESTED = int(
        temporalio.bridge.proto.child_workflow.ChildWorkflowCancellationType.WAIT_CANCELLATION_REQUESTED
    )


class ParentClosePolicy(IntEnum):
    """How a child workflow should be handled when the parent closes."""

    UNSPECIFIED = int(
        temporalio.bridge.proto.child_workflow.ParentClosePolicy.PARENT_CLOSE_POLICY_UNSPECIFIED
    )
    TERMINATE = int(
        temporalio.bridge.proto.child_workflow.ParentClosePolicy.PARENT_CLOSE_POLICY_TERMINATE
    )
    ABANDON = int(
        temporalio.bridge.proto.child_workflow.ParentClosePolicy.PARENT_CLOSE_POLICY_ABANDON
    )
    REQUEST_CANCEL = int(
        temporalio.bridge.proto.child_workflow.ParentClosePolicy.PARENT_CLOSE_POLICY_REQUEST_CANCEL
    )


class ChildWorkflowConfig(TypedDict, total=False):
    """TypedDict of config that can be used for :py:func:`start_child_workflow`
    and :py:func:`execute_child_workflow`.
    """

    id: str
    task_queue: Optional[str]
    namespace: Optional[str]
    cancellation_type: ChildWorkflowCancellationType
    parent_close_policy: ParentClosePolicy
    execution_timeout: Optional[timedelta]
    run_timeout: Optional[timedelta]
    task_timeout: Optional[timedelta]
    id_reuse_policy: temporalio.common.WorkflowIDReusePolicy
    retry_policy: Optional[temporalio.common.RetryPolicy]
    cron_schedule: str
    memo: Optional[Mapping[str, Any]]
    search_attributes: Optional[Mapping[str, Any]]


# Overload for no-param workflow
@overload
async def start_child_workflow(
    workflow: Callable[[ChildWorkflowClass], Awaitable[WorkflowReturnType]],
    /,
    *,
    id: str,
    task_queue: Optional[str] = None,
    namespace: Optional[str] = None,
    cancellation_type: ChildWorkflowCancellationType = ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
    parent_close_policy: ParentClosePolicy = ParentClosePolicy.TERMINATE,
    execution_timeout: Optional[timedelta] = None,
    run_timeout: Optional[timedelta] = None,
    task_timeout: Optional[timedelta] = None,
    id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cron_schedule: str = "",
    memo: Optional[Mapping[str, Any]] = None,
    search_attributes: Optional[Mapping[str, Any]] = None,
) -> ChildWorkflowHandle[ChildWorkflowClass, WorkflowReturnType]:
    ...


# Overload for single-param workflow
@overload
async def start_child_workflow(
    workflow: Callable[
        [ChildWorkflowClass, LocalParamType], Awaitable[WorkflowReturnType]
    ],
    arg: LocalParamType,
    /,
    *,
    id: str,
    task_queue: Optional[str] = None,
    namespace: Optional[str] = None,
    cancellation_type: ChildWorkflowCancellationType = ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
    parent_close_policy: ParentClosePolicy = ParentClosePolicy.TERMINATE,
    execution_timeout: Optional[timedelta] = None,
    run_timeout: Optional[timedelta] = None,
    task_timeout: Optional[timedelta] = None,
    id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cron_schedule: str = "",
    memo: Optional[Mapping[str, Any]] = None,
    search_attributes: Optional[Mapping[str, Any]] = None,
) -> ChildWorkflowHandle[ChildWorkflowClass, WorkflowReturnType]:
    ...


# Overload for string-name workflow
@overload
async def start_child_workflow(
    workflow: str,
    *args: Any,
    id: str,
    task_queue: Optional[str] = None,
    namespace: Optional[str] = None,
    cancellation_type: ChildWorkflowCancellationType = ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
    parent_close_policy: ParentClosePolicy = ParentClosePolicy.TERMINATE,
    execution_timeout: Optional[timedelta] = None,
    run_timeout: Optional[timedelta] = None,
    task_timeout: Optional[timedelta] = None,
    id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cron_schedule: str = "",
    memo: Optional[Mapping[str, Any]] = None,
    search_attributes: Optional[Mapping[str, Any]] = None,
) -> ChildWorkflowHandle[Any, Any]:
    ...


async def start_child_workflow(
    workflow: Any,
    *args: Any,
    id: str,
    task_queue: Optional[str] = None,
    namespace: Optional[str] = None,
    cancellation_type: ChildWorkflowCancellationType = ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
    parent_close_policy: ParentClosePolicy = ParentClosePolicy.TERMINATE,
    execution_timeout: Optional[timedelta] = None,
    run_timeout: Optional[timedelta] = None,
    task_timeout: Optional[timedelta] = None,
    id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cron_schedule: str = "",
    memo: Optional[Mapping[str, Any]] = None,
    search_attributes: Optional[Mapping[str, Any]] = None,
) -> ChildWorkflowHandle[Any, Any]:
    """Start a child workflow and return its handle.

    Args:
        workflow: String name or class method decorated with ``@workflow.run``
            for the workflow to start.
        args: Arguments for the workflow if any.
        id: Unique identifier for the workflow execution.
        task_queue: Task queue to run the workflow on. Defaults to the current
            workflow's task queue.
        namespace: Namespace to run the child workflow on. Defaults to the
            current workflow's namespace.
        cancellation_type: How the child workflow will react to cancellation.
        parent_close_policy: How to handle the child workflow when the parent
            workflow closes.
        execution_timeout: Total workflow execution timeout including
            retries and continue as new.
        run_timeout: Timeout of a single workflow run.
        task_timeout: Timeout of a single workflow task.
        id_reuse_policy: How already-existing IDs are treated.
        retry_policy: Retry policy for the workflow.
        cron_schedule: See https://docs.temporal.io/docs/content/what-is-a-temporal-cron-job/
        memo: Memo for the workflow.
        search_attributes: Search attributes for the workflow.

    Returns:
        A workflow handle to the started/existing workflow.
    """
    return await _Runtime.current().workflow_start_child_workflow(
        workflow,
        *args,
        id=id,
        task_queue=task_queue,
        namespace=namespace,
        cancellation_type=cancellation_type,
        parent_close_policy=parent_close_policy,
        execution_timeout=execution_timeout,
        run_timeout=run_timeout,
        task_timeout=task_timeout,
        id_reuse_policy=id_reuse_policy,
        retry_policy=retry_policy,
        cron_schedule=cron_schedule,
        memo=memo,
        search_attributes=search_attributes,
    )


# Overload for no-param workflow
@overload
async def execute_child_workflow(
    workflow: Callable[[ChildWorkflowClass], Awaitable[WorkflowReturnType]],
    /,
    *,
    id: str,
    task_queue: Optional[str] = None,
    namespace: Optional[str] = None,
    cancellation_type: ChildWorkflowCancellationType = ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
    parent_close_policy: ParentClosePolicy = ParentClosePolicy.TERMINATE,
    execution_timeout: Optional[timedelta] = None,
    run_timeout: Optional[timedelta] = None,
    task_timeout: Optional[timedelta] = None,
    id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cron_schedule: str = "",
    memo: Optional[Mapping[str, Any]] = None,
    search_attributes: Optional[Mapping[str, Any]] = None,
) -> WorkflowReturnType:
    ...


# Overload for single-param workflow
@overload
async def execute_child_workflow(
    workflow: Callable[
        [ChildWorkflowClass, LocalParamType], Awaitable[WorkflowReturnType]
    ],
    arg: LocalParamType,
    /,
    *,
    id: str,
    task_queue: Optional[str] = None,
    namespace: Optional[str] = None,
    cancellation_type: ChildWorkflowCancellationType = ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
    parent_close_policy: ParentClosePolicy = ParentClosePolicy.TERMINATE,
    execution_timeout: Optional[timedelta] = None,
    run_timeout: Optional[timedelta] = None,
    task_timeout: Optional[timedelta] = None,
    id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cron_schedule: str = "",
    memo: Optional[Mapping[str, Any]] = None,
    search_attributes: Optional[Mapping[str, Any]] = None,
) -> WorkflowReturnType:
    ...


# Overload for string-name workflow
@overload
async def execute_child_workflow(
    workflow: str,
    *args: Any,
    id: str,
    task_queue: Optional[str] = None,
    namespace: Optional[str] = None,
    cancellation_type: ChildWorkflowCancellationType = ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
    parent_close_policy: ParentClosePolicy = ParentClosePolicy.TERMINATE,
    execution_timeout: Optional[timedelta] = None,
    run_timeout: Optional[timedelta] = None,
    task_timeout: Optional[timedelta] = None,
    id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cron_schedule: str = "",
    memo: Optional[Mapping[str, Any]] = None,
    search_attributes: Optional[Mapping[str, Any]] = None,
) -> Any:
    ...


async def execute_child_workflow(
    workflow: Any,
    *args: Any,
    id: str,
    task_queue: Optional[str] = None,
    namespace: Optional[str] = None,
    cancellation_type: ChildWorkflowCancellationType = ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
    parent_close_policy: ParentClosePolicy = ParentClosePolicy.TERMINATE,
    execution_timeout: Optional[timedelta] = None,
    run_timeout: Optional[timedelta] = None,
    task_timeout: Optional[timedelta] = None,
    id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cron_schedule: str = "",
    memo: Optional[Mapping[str, Any]] = None,
    search_attributes: Optional[Mapping[str, Any]] = None,
) -> Any:
    """Start a child workflow and wait for completion.

    This is a shortcut for ``await`` :py:meth:`start_child_workflow`.
    """
    # We call the runtime directly instead of top-level start_child_workflow to
    # ensure we don't miss new parameters
    handle = await _Runtime.current().workflow_start_child_workflow(
        workflow,
        *args,
        id=id,
        task_queue=task_queue,
        namespace=namespace,
        cancellation_type=cancellation_type,
        parent_close_policy=parent_close_policy,
        execution_timeout=execution_timeout,
        run_timeout=run_timeout,
        task_timeout=task_timeout,
        id_reuse_policy=id_reuse_policy,
        retry_policy=retry_policy,
        cron_schedule=cron_schedule,
        memo=memo,
        search_attributes=search_attributes,
    )
    return await handle


def _is_unbound_method_on_cls(fn: Callable[..., Any], cls: Type) -> bool:
    # Python 3 does not make this easy, ref https://stackoverflow.com/questions/3589311
    return (
        inspect.isfunction(fn)
        and inspect.getmodule(fn) is inspect.getmodule(cls)
        and fn.__qualname__.rsplit(".", 1)[0] == cls.__name__
    )
