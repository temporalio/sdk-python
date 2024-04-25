"""Utilities that can decorate or be called inside workflows."""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import uuid
import warnings
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum, IntEnum
from functools import partial
from random import Random
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    Generic,
    Iterator,
    List,
    Mapping,
    MutableMapping,
    NoReturn,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
    cast,
    overload,
)

from typing_extensions import (
    Concatenate,
    Literal,
    Protocol,
    TypedDict,
    runtime_checkable,
)

import temporalio.api.common.v1
import temporalio.bridge.proto.child_workflow
import temporalio.bridge.proto.workflow_commands
import temporalio.common
import temporalio.converter
import temporalio.exceptions

from .types import (
    AnyType,
    CallableAsyncNoParam,
    CallableAsyncSingleParam,
    CallableAsyncType,
    CallableSyncNoParam,
    CallableSyncOrAsyncReturnNoneType,
    CallableSyncOrAsyncType,
    CallableSyncSingleParam,
    CallableType,
    ClassType,
    MethodAsyncNoParam,
    MethodAsyncSingleParam,
    MethodSyncNoParam,
    MethodSyncOrAsyncNoParam,
    MethodSyncOrAsyncSingleParam,
    MethodSyncSingleParam,
    MultiParamSpec,
    ParamType,
    ProtocolReturnType,
    ReturnType,
    SelfType,
)


@overload
def defn(cls: ClassType) -> ClassType:
    ...


@overload
def defn(
    *,
    name: Optional[str] = None,
    sandboxed: bool = True,
    failure_exception_types: Sequence[Type[BaseException]] = [],
) -> Callable[[ClassType], ClassType]:
    ...


@overload
def defn(
    *, sandboxed: bool = True, dynamic: bool = False
) -> Callable[[ClassType], ClassType]:
    ...


def defn(
    cls: Optional[ClassType] = None,
    *,
    name: Optional[str] = None,
    sandboxed: bool = True,
    dynamic: bool = False,
    failure_exception_types: Sequence[Type[BaseException]] = [],
):
    """Decorator for workflow classes.

    This must be set on any registered workflow class (it is ignored if on a
    base class).

    Args:
        cls: The class to decorate.
        name: Name to use for the workflow. Defaults to class ``__name__``. This
            cannot be set if dynamic is set.
        sandboxed: Whether the workflow should run in a sandbox. Default is
            true.
        dynamic: If true, this activity will be dynamic. Dynamic workflows have
            to accept a single 'Sequence[RawValue]' parameter. This cannot be
            set to true if name is present.
        failure_exception_types: The types of exceptions that, if a
            workflow-thrown exception extends, will cause the workflow/update to
            fail instead of suspending the workflow via task failure. These are
            applied in addition to ones set on the worker constructor. If
            ``Exception`` is set, it effectively will fail a workflow/update in
            all user exception cases. WARNING: This setting is experimental.
    """

    def decorator(cls: ClassType) -> ClassType:
        # This performs validation
        _Definition._apply_to_class(
            cls,
            workflow_name=name or cls.__name__ if not dynamic else None,
            sandboxed=sandboxed,
            failure_exception_types=failure_exception_types,
        )
        return cls

    if cls is not None:
        return decorator(cls)
    return decorator


def run(fn: CallableAsyncType) -> CallableAsyncType:
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
    # Disallow local classes because we need to have the class globally
    # referenceable by name
    if "<locals>" in fn.__qualname__:
        raise ValueError(
            "Local classes unsupported, @workflow.run cannot be on a local class"
        )
    setattr(fn, "__temporal_workflow_run", True)
    # TODO(cretz): Why is MyPy unhappy with this return?
    return fn  # type: ignore[return-value]


@overload
def signal(fn: CallableSyncOrAsyncReturnNoneType) -> CallableSyncOrAsyncReturnNoneType:
    ...


@overload
def signal(
    *, name: str
) -> Callable[[CallableSyncOrAsyncReturnNoneType], CallableSyncOrAsyncReturnNoneType]:
    ...


@overload
def signal(
    *, dynamic: Literal[True]
) -> Callable[[CallableSyncOrAsyncReturnNoneType], CallableSyncOrAsyncReturnNoneType]:
    ...


def signal(
    fn: Optional[CallableSyncOrAsyncReturnNoneType] = None,
    *,
    name: Optional[str] = None,
    dynamic: Optional[bool] = False,
):
    """Decorator for a workflow signal method.

    This is set on any async or non-async method that you wish to be called upon
    receiving a signal. If a function overrides one with this decorator, it too
    must be decorated.

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

    def with_name(
        name: Optional[str], fn: CallableSyncOrAsyncReturnNoneType
    ) -> CallableSyncOrAsyncReturnNoneType:
        defn = _SignalDefinition(name=name, fn=fn, is_method=True)
        setattr(fn, "__temporal_signal_definition", defn)
        if defn.dynamic_vararg:
            warnings.warn(
                "Dynamic signals with vararg third param is deprecated, use Sequence[RawValue]",
                DeprecationWarning,
                stacklevel=2,
            )
        return fn

    if name is not None or dynamic:
        if name is not None and dynamic:
            raise RuntimeError("Cannot provide name and dynamic boolean")
        return partial(with_name, name)
    if fn is None:
        raise RuntimeError("Cannot create signal without function or name or dynamic")
    return with_name(fn.__name__, fn)


@overload
def query(fn: CallableType) -> CallableType:
    ...


@overload
def query(*, name: str) -> Callable[[CallableType], CallableType]:
    ...


@overload
def query(*, dynamic: Literal[True]) -> Callable[[CallableType], CallableType]:
    ...


def query(
    fn: Optional[CallableType] = None,
    *,
    name: Optional[str] = None,
    dynamic: Optional[bool] = False,
):
    """Decorator for a workflow query method.

    This is set on any non-async method that expects to handle a query. If a
    function overrides one with this decorator, it too must be decorated.

    Query methods can only have positional parameters. Best practice for
    non-dynamic query methods is to only take a single object/dataclass
    argument that can accept more fields later if needed. The return value is
    the resulting query value. Query methods must not mutate any workflow state.

    Args:
        fn: The function to decorate.
        name: Query name. Defaults to method ``__name__``. Cannot be present
            when ``dynamic`` is present.
        dynamic: If true, this handles all queries not otherwise handled. The
            parameters of the method should be self, a string name, and a
            ``Sequence[RawValue]``. An older form of this accepted vararg
            parameters which will now warn. Cannot be present when ``name`` is
            present.
    """

    def with_name(
        name: Optional[str], fn: CallableType, *, bypass_async_check: bool = False
    ) -> CallableType:
        if not bypass_async_check and inspect.iscoroutinefunction(fn):
            warnings.warn(
                "Queries as async def functions are deprecated",
                DeprecationWarning,
                stacklevel=2,
            )
        defn = _QueryDefinition(name=name, fn=fn, is_method=True)
        setattr(fn, "__temporal_query_definition", defn)
        if defn.dynamic_vararg:
            warnings.warn(
                "Dynamic queries with vararg third param is deprecated, use Sequence[RawValue]",
                DeprecationWarning,
                stacklevel=2,
            )
        return fn

    if name is not None or dynamic:
        if name is not None and dynamic:
            raise RuntimeError("Cannot provide name and dynamic boolean")
        return partial(with_name, name)
    if fn is None:
        raise RuntimeError("Cannot create query without function or name or dynamic")
    if inspect.iscoroutinefunction(fn):
        warnings.warn(
            "Queries as async def functions are deprecated",
            DeprecationWarning,
            stacklevel=2,
        )
    return with_name(fn.__name__, fn, bypass_async_check=True)


@dataclass(frozen=True)
class Info:
    """Information about the running workflow.

    Retrieved inside a workflow via :py:func:`info`. This object is immutable
    with the exception of the :py:attr:`search_attributes` and
    :py:attr:`typed_search_attributes` which is updated on
    :py:func:`upsert_search_attributes`.

    Note, required fields may be added here in future versions. This class
    should never be constructed by users.
    """

    attempt: int
    continued_run_id: Optional[str]
    cron_schedule: Optional[str]
    execution_timeout: Optional[timedelta]
    headers: Mapping[str, temporalio.api.common.v1.Payload]
    namespace: str
    parent: Optional[ParentInfo]
    raw_memo: Mapping[str, temporalio.api.common.v1.Payload]
    retry_policy: Optional[temporalio.common.RetryPolicy]
    run_id: str
    run_timeout: Optional[timedelta]

    search_attributes: temporalio.common.SearchAttributes
    """Search attributes for the workflow.

    .. deprecated::
        Use :py:attr:`typed_search_attributes` instead.
    """

    start_time: datetime
    task_queue: str
    task_timeout: timedelta

    typed_search_attributes: temporalio.common.TypedSearchAttributes
    """Search attributes for the workflow.
    
    Note, this may have invalid values or be missing values if passing the
    deprecated form of dictionary attributes to
    :py:meth:`upsert_search_attributes`.
    """

    workflow_id: str
    workflow_type: str

    def _logger_details(self) -> Mapping[str, Any]:
        return {
            # TODO(cretz): worker ID?
            "attempt": self.attempt,
            "namespace": self.namespace,
            "run_id": self.run_id,
            "task_queue": self.task_queue,
            "workflow_id": self.workflow_id,
            "workflow_type": self.workflow_type,
        }

    def get_current_build_id(self) -> str:
        """Get the Build ID of the worker which executed the current Workflow Task.

        May be undefined if the task was completed by a worker without a Build ID. If this worker is the one executing
        this task for the first time and has a Build ID set, then its ID will be used. This value may change over the
        lifetime of the workflow run, but is deterministic and safe to use for branching.
        """
        return _Runtime.current().workflow_get_current_build_id()

    def get_current_history_length(self) -> int:
        """Get the current number of events in history.

        Note, this value may not be up to date if accessed inside a query.

        Returns:
            Current number of events in history (up until the current task).
        """
        return _Runtime.current().workflow_get_current_history_length()

    def get_current_history_size(self) -> int:
        """Get the current byte size of history.

        Note, this value may not be up to date if accessed inside a query.

        Returns:
            Current byte-size of history (up until the current task).
        """
        return _Runtime.current().workflow_get_current_history_size()

    def is_continue_as_new_suggested(self) -> bool:
        """Get whether or not continue as new is suggested.

        Note, this value may not be up to date if accessed inside a query.

        Returns:
            True if the server is configured to suggest continue as new and it
            is suggested.
        """
        return _Runtime.current().workflow_is_continue_as_new_suggested()


@dataclass(frozen=True)
class ParentInfo:
    """Information about the parent workflow."""

    namespace: str
    run_id: str
    workflow_id: str


class _Runtime(ABC):
    @staticmethod
    def current() -> _Runtime:
        loop = _Runtime.maybe_current()
        if not loop:
            raise _NotInWorkflowEventLoopError("Not in workflow event loop")
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
    def workflow_continue_as_new(
        self,
        *args: Any,
        workflow: Union[None, Callable, str],
        task_queue: Optional[str],
        run_timeout: Optional[timedelta],
        task_timeout: Optional[timedelta],
        retry_policy: Optional[temporalio.common.RetryPolicy],
        memo: Optional[Mapping[str, Any]],
        search_attributes: Optional[
            Union[
                temporalio.common.SearchAttributes,
                temporalio.common.TypedSearchAttributes,
            ]
        ],
        versioning_intent: Optional[VersioningIntent],
    ) -> NoReturn:
        ...

    @abstractmethod
    def workflow_extern_functions(self) -> Mapping[str, Callable]:
        ...

    @abstractmethod
    def workflow_get_current_build_id(self) -> str:
        ...

    @abstractmethod
    def workflow_get_current_history_length(self) -> int:
        ...

    @abstractmethod
    def workflow_get_current_history_size(self) -> int:
        ...

    @abstractmethod
    def workflow_get_external_workflow_handle(
        self, id: str, *, run_id: Optional[str]
    ) -> ExternalWorkflowHandle[Any]:
        ...

    @abstractmethod
    def workflow_get_query_handler(self, name: Optional[str]) -> Optional[Callable]:
        ...

    @abstractmethod
    def workflow_get_signal_handler(self, name: Optional[str]) -> Optional[Callable]:
        ...

    @abstractmethod
    def workflow_get_update_handler(self, name: Optional[str]) -> Optional[Callable]:
        ...

    @abstractmethod
    def workflow_get_update_validator(self, name: Optional[str]) -> Optional[Callable]:
        ...

    @abstractmethod
    def workflow_info(self) -> Info:
        ...

    @abstractmethod
    def workflow_is_continue_as_new_suggested(self) -> bool:
        ...

    @abstractmethod
    def workflow_is_replaying(self) -> bool:
        ...

    @abstractmethod
    def workflow_memo(self) -> Mapping[str, Any]:
        ...

    @abstractmethod
    def workflow_memo_value(
        self, key: str, default: Any, *, type_hint: Optional[Type]
    ) -> Any:
        ...

    @abstractmethod
    def workflow_metric_meter(self) -> temporalio.common.MetricMeter:
        ...

    @abstractmethod
    def workflow_patch(self, id: str, *, deprecated: bool) -> bool:
        ...

    @abstractmethod
    def workflow_payload_converter(self) -> temporalio.converter.PayloadConverter:
        ...

    @abstractmethod
    def workflow_random(self) -> Random:
        ...

    @abstractmethod
    def workflow_set_query_handler(
        self, name: Optional[str], handler: Optional[Callable]
    ) -> None:
        ...

    @abstractmethod
    def workflow_set_signal_handler(
        self, name: Optional[str], handler: Optional[Callable]
    ) -> None:
        ...

    @abstractmethod
    def workflow_set_update_handler(
        self,
        name: Optional[str],
        handler: Optional[Callable],
        validator: Optional[Callable],
    ) -> None:
        ...

    @abstractmethod
    def workflow_start_activity(
        self,
        activity: Any,
        *args: Any,
        task_queue: Optional[str],
        result_type: Optional[Type],
        schedule_to_close_timeout: Optional[timedelta],
        schedule_to_start_timeout: Optional[timedelta],
        start_to_close_timeout: Optional[timedelta],
        heartbeat_timeout: Optional[timedelta],
        retry_policy: Optional[temporalio.common.RetryPolicy],
        cancellation_type: ActivityCancellationType,
        activity_id: Optional[str],
        versioning_intent: Optional[VersioningIntent],
    ) -> ActivityHandle[Any]:
        ...

    @abstractmethod
    async def workflow_start_child_workflow(
        self,
        workflow: Any,
        *args: Any,
        id: str,
        task_queue: Optional[str],
        result_type: Optional[Type],
        cancellation_type: ChildWorkflowCancellationType,
        parent_close_policy: ParentClosePolicy,
        execution_timeout: Optional[timedelta],
        run_timeout: Optional[timedelta],
        task_timeout: Optional[timedelta],
        id_reuse_policy: temporalio.common.WorkflowIDReusePolicy,
        retry_policy: Optional[temporalio.common.RetryPolicy],
        cron_schedule: str,
        memo: Optional[Mapping[str, Any]],
        search_attributes: Optional[
            Union[
                temporalio.common.SearchAttributes,
                temporalio.common.TypedSearchAttributes,
            ]
        ],
        versioning_intent: Optional[VersioningIntent],
    ) -> ChildWorkflowHandle[Any, Any]:
        ...

    @abstractmethod
    def workflow_start_local_activity(
        self,
        activity: Any,
        *args: Any,
        result_type: Optional[Type],
        schedule_to_close_timeout: Optional[timedelta],
        schedule_to_start_timeout: Optional[timedelta],
        start_to_close_timeout: Optional[timedelta],
        retry_policy: Optional[temporalio.common.RetryPolicy],
        local_retry_threshold: Optional[timedelta],
        cancellation_type: ActivityCancellationType,
        activity_id: Optional[str],
    ) -> ActivityHandle[Any]:
        ...

    @abstractmethod
    def workflow_time_ns(self) -> int:
        ...

    @abstractmethod
    def workflow_upsert_search_attributes(
        self,
        attributes: Union[
            temporalio.common.SearchAttributes,
            Sequence[temporalio.common.SearchAttributeUpdate],
        ],
    ) -> None:
        ...

    @abstractmethod
    async def workflow_wait_condition(
        self, fn: Callable[[], bool], *, timeout: Optional[float] = None
    ) -> None:
        ...


def deprecate_patch(id: str) -> None:
    """Mark a patch as deprecated.

    This marks a workflow that had :py:func:`patched` in a previous version of
    the code as no longer applicable because all workflows that use the old code
    path are done and will never be queried again. Therefore the old code path
    is removed as well.

    Args:
        id: The identifier originally used with :py:func:`patched`.
    """
    _Runtime.current().workflow_patch(id, deprecated=True)


def extern_functions() -> Mapping[str, Callable]:
    """External functions available in the workflow sandbox.

    Returns:
        Mapping of external functions that can be called from inside a workflow
        sandbox.
    """
    return _Runtime.current().workflow_extern_functions()


def info() -> Info:
    """Current workflow's info.

    Returns:
        Info for the currently running workflow.
    """
    return _Runtime.current().workflow_info()


def memo() -> Mapping[str, Any]:
    """Current workflow's memo values, converted without type hints.

    Since type hints are not used, the default converted values will come back.
    For example, if the memo was originally created with a dataclass, the value
    will be a dict. To convert using proper type hints, use
    :py:func:`memo_value`.

    Returns:
        Mapping of all memo keys and they values without type hints.
    """
    return _Runtime.current().workflow_memo()


@overload
def memo_value(key: str, default: Any = temporalio.common._arg_unset) -> Any:
    ...


@overload
def memo_value(key: str, *, type_hint: Type[ParamType]) -> ParamType:
    ...


@overload
def memo_value(
    key: str, default: AnyType, *, type_hint: Type[ParamType]
) -> Union[AnyType, ParamType]:
    ...


def memo_value(
    key: str,
    default: Any = temporalio.common._arg_unset,
    *,
    type_hint: Optional[Type] = None,
) -> Any:
    """Memo value for the given key, optional default, and optional type
    hint.

    Args:
        key: Key to get memo value for.
        default: Default to use if key is not present. If unset, a
            :py:class:`KeyError` is raised when the key does not exist.
        type_hint: Type hint to use when converting.

    Returns:
        Memo value, converted with the type hint if present.

    Raises:
        KeyError: Key not present and default not set.
    """
    return _Runtime.current().workflow_memo_value(key, default, type_hint=type_hint)


def metric_meter() -> temporalio.common.MetricMeter:
    """Get the metric meter for the current workflow.

    This meter is replay safe which means that metrics will not be recorded
    during replay.

    Returns:
        Current metric meter for this workflow for recording metrics.
    """
    return _Runtime.current().workflow_metric_meter()


def now() -> datetime:
    """Current time from the workflow perspective.

    This is the workflow equivalent of :py:func:`datetime.now` with the
    :py:attr:`timezone.utc` parameter.

    Returns:
        UTC datetime for the current workflow time. The datetime does have UTC
        set as the time zone.
    """
    return datetime.fromtimestamp(time(), timezone.utc)


def patched(id: str) -> bool:
    """Patch a workflow.

    When called, this will only return true if code should take the newer path
    which means this is either not replaying or is replaying and has seen this
    patch before.

    Use :py:func:`deprecate_patch` when all workflows are done and will never be
    queried again. The old code path can be used at that time too.

    Args:
        id: The identifier for this patch. This identifier may be used
            repeatedly in the same workflow to represent the same patch

    Returns:
        True if this should take the newer path, false if it should take the
        older path.
    """
    return _Runtime.current().workflow_patch(id, deprecated=False)


def payload_converter() -> temporalio.converter.PayloadConverter:
    """Get the payload converter for the current workflow.

    This is often used for dynamic workflows/signals/queries to convert
    payloads.
    """
    return _Runtime.current().workflow_payload_converter()


def random() -> Random:
    """Get a deterministic pseudo-random number generator.

    Note, this random number generator is not cryptographically safe and should
    not be used for security purposes.

    Returns:
        The deterministically-seeded pseudo-random number generator.
    """
    return _Runtime.current().workflow_random()


def time() -> float:
    """Current seconds since the epoch from the workflow perspective.

    This is the workflow equivalent of :py:func:`time.time`.

    Returns:
        Seconds since the epoch as a float.
    """
    return time_ns() / 1e9


def time_ns() -> int:
    """Current nanoseconds since the epoch from the workflow perspective.

    This is the workflow equivalent of :py:func:`time.time_ns`.

    Returns:
        Nanoseconds since the epoch
    """
    return _Runtime.current().workflow_time_ns()


def upsert_search_attributes(
    attributes: Union[
        temporalio.common.SearchAttributes,
        Sequence[temporalio.common.SearchAttributeUpdate],
    ]
) -> None:
    """Upsert search attributes for this workflow.

    Args:
        attributes: The attributes to set. This should be a sequence of
            updates (i.e. values created via value_set and value_unset calls on
            search attribute keys). The dictionary form of attributes is
            DEPRECATED and if used, result in invalid key types on the
            typed_search_attributes property in the info.
    """
    temporalio.common._warn_on_deprecated_search_attributes(attributes)
    _Runtime.current().workflow_upsert_search_attributes(attributes)


# Needs to be defined here to avoid a circular import
@runtime_checkable
class UpdateMethodMultiParam(Protocol[MultiParamSpec, ProtocolReturnType]):
    """Decorated workflow update functions implement this."""

    _defn: temporalio.workflow._UpdateDefinition

    def __call__(
        self, *args: MultiParamSpec.args, **kwargs: MultiParamSpec.kwargs
    ) -> Union[ProtocolReturnType, Awaitable[ProtocolReturnType]]:
        """Generic callable type callback."""
        ...

    def validator(
        self, vfunc: Callable[MultiParamSpec, None]
    ) -> Callable[MultiParamSpec, None]:
        """Use to decorate a function to validate the arguments passed to the update handler."""
        ...


@overload
def update(
    fn: Callable[MultiParamSpec, Awaitable[ReturnType]]
) -> UpdateMethodMultiParam[MultiParamSpec, ReturnType]:
    ...


@overload
def update(
    fn: Callable[MultiParamSpec, ReturnType]
) -> UpdateMethodMultiParam[MultiParamSpec, ReturnType]:
    ...


@overload
def update(
    *, name: str
) -> Callable[
    [Callable[MultiParamSpec, ReturnType]],
    UpdateMethodMultiParam[MultiParamSpec, ReturnType],
]:
    ...


@overload
def update(
    *, dynamic: Literal[True]
) -> Callable[
    [Callable[MultiParamSpec, ReturnType]],
    UpdateMethodMultiParam[MultiParamSpec, ReturnType],
]:
    ...


def update(
    fn: Optional[CallableSyncOrAsyncType] = None,
    *,
    name: Optional[str] = None,
    dynamic: Optional[bool] = False,
):
    """Decorator for a workflow update handler method.

    This is set on any async or non-async method that you wish to be called upon
    receiving an update. If a function overrides one with this decorator, it too
    must be decorated.

    You may also optionally define a validator method that will be called before
    this handler you have applied this decorator to. You can specify the validator
    with ``@update_handler_function_name.validator``.

    Update methods can only have positional parameters. Best practice for
    non-dynamic update methods is to only take a single object/dataclass
    argument that can accept more fields later if needed. The handler may return
    a serializable value which will be sent back to the caller of the update.

    .. warning::
       This API is experimental

    Args:
        fn: The function to decorate.
        name: Update name. Defaults to method ``__name__``. Cannot be present
            when ``dynamic`` is present.
        dynamic: If true, this handles all updates not otherwise handled. The
            parameters of the method must be self, a string name, and a
            ``*args`` positional varargs. Cannot be present when ``name`` is
            present.
    """

    def with_name(
        name: Optional[str], fn: CallableSyncOrAsyncType
    ) -> CallableSyncOrAsyncType:
        defn = _UpdateDefinition(name=name, fn=fn, is_method=True)
        if defn.dynamic_vararg:
            raise RuntimeError(
                "Dynamic updates do not support a vararg third param, use Sequence[RawValue]",
            )
        setattr(fn, "_defn", defn)
        setattr(fn, "validator", partial(_update_validator, defn))
        return fn

    if name is not None or dynamic:
        if name is not None and dynamic:
            raise RuntimeError("Cannot provide name and dynamic boolean")
        return partial(with_name, name)
    if fn is None:
        raise RuntimeError("Cannot create update without function or name or dynamic")
    return with_name(fn.__name__, fn)


def _update_validator(
    update_def: _UpdateDefinition, fn: Optional[Callable[..., None]] = None
) -> Optional[Callable[..., None]]:
    """Decorator for a workflow update validator method."""
    if fn is not None:
        update_def.set_validator(fn)
    return fn


def uuid4() -> uuid.UUID:
    """Get a new, determinism-safe v4 UUID based on :py:func:`random`.

    Note, this UUID is not cryptographically safe and should not be used for
    security purposes.

    Returns:
        A deterministically-seeded v4 UUID.
    """
    return uuid.UUID(bytes=random().getrandbits(16 * 8).to_bytes(16, "big"), version=4)


async def wait_condition(
    fn: Callable[[], bool], *, timeout: Optional[Union[timedelta, float]] = None
) -> None:
    """Wait on a callback to become true.

    This function returns when the callback returns true (invoked each loop
    iteration) or the timeout has been reached.

    Args:
        fn: Non-async callback that accepts no parameters and returns a boolean.
        timeout: Optional number of seconds to wait until throwing
            :py:class:`asyncio.TimeoutError`.
    """
    await _Runtime.current().workflow_wait_condition(
        fn,
        timeout=timeout.total_seconds() if isinstance(timeout, timedelta) else timeout,
    )


_sandbox_unrestricted = threading.local()
_in_sandbox = threading.local()
_imports_passed_through = threading.local()


class unsafe:
    """Contains static methods that should not normally be called during
    workflow execution except in advanced cases.
    """

    def __init__(self) -> None:  # noqa: D107
        raise NotImplementedError

    @staticmethod
    def in_sandbox() -> bool:
        """Whether the code is executing on a sandboxed thread.

        Returns:
            True if the code is executing in the sandbox thread.
        """
        return getattr(_in_sandbox, "value", False)

    @staticmethod
    def _set_in_sandbox(v: bool) -> None:
        _in_sandbox.value = v

    @staticmethod
    def is_replaying() -> bool:
        """Whether the workflow is currently replaying.

        Returns:
            True if the workflow is currently replaying
        """
        return _Runtime.current().workflow_is_replaying()

    @staticmethod
    def is_sandbox_unrestricted() -> bool:
        """Whether the current block of code is not restricted via sandbox.

        Returns:
            True if the current code is not restricted in the sandbox.
        """
        # Activations happen in different threads than init and possibly the
        # local hasn't been initialized in _that_ thread, so we allow unset here
        # instead of just setting value = False globally.
        return getattr(_sandbox_unrestricted, "value", False)

    @staticmethod
    @contextmanager
    def sandbox_unrestricted() -> Iterator[None]:
        """A context manager to run code without sandbox restrictions."""
        # Only apply if not already applied. Nested calls just continue
        # unrestricted.
        if unsafe.is_sandbox_unrestricted():
            yield None
            return
        _sandbox_unrestricted.value = True
        try:
            yield None
        finally:
            _sandbox_unrestricted.value = False

    @staticmethod
    def is_imports_passed_through() -> bool:
        """Whether the current block of code is in
        :py:meth:imports_passed_through.

        Returns:
            True if the current code's imports will be passed through
        """
        # See comment in is_sandbox_unrestricted for why we allow unset instead
        # of just global false.
        return getattr(_imports_passed_through, "value", False)

    @staticmethod
    @contextmanager
    def imports_passed_through() -> Iterator[None]:
        """Context manager to mark all imports that occur within it as passed
        through (meaning not reloaded by the sandbox).
        """
        # Only apply if not already applied. Nested calls just continue
        # passed through.
        if unsafe.is_imports_passed_through():
            yield None
            return
        _imports_passed_through.value = True
        try:
            yield None
        finally:
            _imports_passed_through.value = False


class LoggerAdapter(logging.LoggerAdapter):
    """Adapter that adds details to the log about the running workflow.

    Attributes:
        workflow_info_on_message: Boolean for whether a string representation of
            a dict of some workflow info will be appended to each message.
            Default is True.
        workflow_info_on_extra: Boolean for whether a ``temporal_workflow``
            dictionary value will be added to the ``extra`` dictionary with some
            workflow info, making it present on the ``LogRecord.__dict__`` for
            use by others. Default is True.
        full_workflow_info_on_extra: Boolean for whether a ``workflow_info``
            value will be added to the ``extra`` dictionary with the entire
            workflow info, making it present on the ``LogRecord.__dict__`` for
            use by others. Default is False.
        log_during_replay: Boolean for whether logs should occur during replay.
            Default is False.
    """

    def __init__(
        self, logger: logging.Logger, extra: Optional[Mapping[str, Any]]
    ) -> None:
        """Create the logger adapter."""
        super().__init__(logger, extra or {})
        self.workflow_info_on_message = True
        self.workflow_info_on_extra = True
        self.full_workflow_info_on_extra = False
        self.log_during_replay = False

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> Tuple[Any, MutableMapping[str, Any]]:
        """Override to add workflow details."""
        if (
            self.workflow_info_on_message
            or self.workflow_info_on_extra
            or self.full_workflow_info_on_extra
        ):
            runtime = _Runtime.maybe_current()
            if runtime:
                if self.workflow_info_on_message:
                    msg = f"{msg} ({runtime.logger_details})"
                if self.workflow_info_on_extra:
                    # Extra can be absent or None, this handles both
                    extra = kwargs.get("extra", None) or {}
                    extra["temporal_workflow"] = runtime.logger_details
                    kwargs["extra"] = extra
                if self.full_workflow_info_on_extra:
                    # Extra can be absent or None, this handles both
                    extra = kwargs.get("extra", None) or {}
                    extra["workflow_info"] = runtime.workflow_info()
                    kwargs["extra"] = extra
        return (msg, kwargs)

    def isEnabledFor(self, level: int) -> bool:
        """Override to ignore replay logs."""
        if not self.log_during_replay and unsafe.is_replaying():
            return False
        return super().isEnabledFor(level)

    @property
    def base_logger(self) -> logging.Logger:
        """Underlying logger usable for actions such as adding
        handlers/formatters.
        """
        return self.logger


logger = LoggerAdapter(logging.getLogger(__name__), None)
"""Logger that will have contextual workflow details embedded.

Logs are skipped during replay by default.
"""


@dataclass(frozen=True)
class _Definition:
    name: Optional[str]
    cls: Type
    run_fn: Callable[..., Awaitable]
    signals: Mapping[Optional[str], _SignalDefinition]
    queries: Mapping[Optional[str], _QueryDefinition]
    updates: Mapping[Optional[str], _UpdateDefinition]
    sandboxed: bool
    failure_exception_types: Sequence[Type[BaseException]]
    # Types loaded on post init if both are None
    arg_types: Optional[List[Type]] = None
    ret_type: Optional[Type] = None

    @staticmethod
    def from_class(cls: Type) -> Optional[_Definition]:
        # We make sure to only return it if it's on _this_ class
        defn = getattr(cls, "__temporal_workflow_definition", None)
        if defn and defn.cls == cls:
            return defn
        return None

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
    def _apply_to_class(
        cls: Type,
        *,
        workflow_name: Optional[str],
        sandboxed: bool,
        failure_exception_types: Sequence[Type[BaseException]],
    ) -> None:
        # Check it's not being doubly applied
        if _Definition.from_class(cls):
            raise ValueError("Class already contains workflow definition")
        issues: List[str] = []

        # Collect run fn and all signal/query/update fns
        members = inspect.getmembers(cls)
        run_fn: Optional[Callable[..., Awaitable[Any]]] = None
        seen_run_attr = False
        signals: Dict[Optional[str], _SignalDefinition] = {}
        queries: Dict[Optional[str], _QueryDefinition] = {}
        updates: Dict[Optional[str], _UpdateDefinition] = {}
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
            elif isinstance(member, UpdateMethodMultiParam):
                update_defn = member._defn
                if update_defn.name in updates:
                    defn_name = update_defn.name or "<dynamic>"
                    issues.append(
                        f"Multiple update methods found for {defn_name} "
                        f"(at least on {name} and {updates[update_defn.name].fn.__name__})"
                    )
                else:
                    updates[update_defn.name] = update_defn

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
                elif isinstance(base_member, UpdateMethodMultiParam):
                    update_defn = base_member._defn
                    if update_defn.name not in updates:
                        issues.append(
                            f"@workflow.update defined on {base_member.__qualname__} but not on the override"
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
            name=workflow_name,
            cls=cls,
            run_fn=run_fn,
            signals=signals,
            queries=queries,
            updates=updates,
            sandboxed=sandboxed,
            failure_exception_types=failure_exception_types,
        )
        setattr(cls, "__temporal_workflow_definition", defn)
        setattr(run_fn, "__temporal_workflow_definition", defn)

    def __post_init__(self) -> None:
        if self.arg_types is None and self.ret_type is None:
            dynamic = self.name is None
            arg_types, ret_type = temporalio.common._type_hints_from_func(self.run_fn)
            # If dynamic, must be a sequence of raw values
            if dynamic and (
                not arg_types
                or len(arg_types) != 1
                or arg_types[0] != Sequence[temporalio.common.RawValue]
            ):
                raise TypeError(
                    "Dynamic workflow must accept a single Sequence[temporalio.common.RawValue]"
                )
            object.__setattr__(self, "arg_types", arg_types)
            object.__setattr__(self, "ret_type", ret_type)


# Async safe version of partial
def _bind_method(obj: Any, fn: Callable[..., Any]) -> Callable[..., Any]:
    # Curry instance on the definition function since that represents an
    # unbound method
    if inspect.iscoroutinefunction(fn):
        # We cannot use functools.partial here because in <= 3.7 that isn't
        # considered an inspect.iscoroutinefunction
        fn = cast(Callable[..., Awaitable[Any]], fn)

        async def with_object(*args, **kwargs) -> Any:
            return await fn(obj, *args, **kwargs)

        return with_object
    return partial(fn, obj)


# Returns true if normal form, false if vararg form
def _assert_dynamic_handler_args(
    fn: Callable, arg_types: Optional[List[Type]], is_method: bool
) -> bool:
    # Dynamic query/signal/update must have three args: self, name, and
    # Sequence[RawValue]. An older form accepted varargs for the third param for signals/queries so
    # we will too (but will warn in the signal/query code).
    params = list(inspect.signature(fn).parameters.values())
    total_expected_params = 3 if is_method else 2
    if (
        len(params) == total_expected_params
        and params[-2].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        and params[-1].kind is inspect.Parameter.VAR_POSITIONAL
    ):
        # Old var-arg form
        return False
    if (
        not arg_types
        or len(arg_types) != 2
        or arg_types[0] != str
        or arg_types[1] != Sequence[temporalio.common.RawValue]
    ):
        raise RuntimeError(
            "Dynamic handler must have 3 arguments: self, str, and Sequence[temporalio.common.RawValue]"
        )
    return True


@dataclass(frozen=True)
class _SignalDefinition:
    # None if dynamic
    name: Optional[str]
    fn: Callable[..., Union[None, Awaitable[None]]]
    is_method: bool
    # Types loaded on post init if None
    arg_types: Optional[List[Type]] = None
    dynamic_vararg: bool = False

    @staticmethod
    def from_fn(fn: Callable) -> Optional[_SignalDefinition]:
        return getattr(fn, "__temporal_signal_definition", None)

    @staticmethod
    def must_name_from_fn_or_str(signal: Union[str, Callable]) -> str:
        if callable(signal):
            defn = _SignalDefinition.from_fn(signal)
            if not defn:
                raise RuntimeError(
                    f"Signal definition not found on {signal.__qualname__}, "
                    "is it decorated with @workflow.signal?"
                )
            elif not defn.name:
                raise RuntimeError("Cannot invoke dynamic signal definition")
            # TODO(cretz): Check count/type of args at runtime?
            return defn.name
        return str(signal)

    def __post_init__(self) -> None:
        if self.arg_types is None:
            arg_types, _ = temporalio.common._type_hints_from_func(self.fn)
            # If dynamic, assert it
            if not self.name:
                object.__setattr__(
                    self,
                    "dynamic_vararg",
                    not _assert_dynamic_handler_args(
                        self.fn, arg_types, self.is_method
                    ),
                )
            object.__setattr__(self, "arg_types", arg_types)

    def bind_fn(self, obj: Any) -> Callable[..., Any]:
        return _bind_method(obj, self.fn)


@dataclass(frozen=True)
class _QueryDefinition:
    # None if dynamic
    name: Optional[str]
    fn: Callable[..., Any]
    is_method: bool
    # Types loaded on post init if both are None
    arg_types: Optional[List[Type]] = None
    ret_type: Optional[Type] = None
    dynamic_vararg: bool = False

    @staticmethod
    def from_fn(fn: Callable) -> Optional[_QueryDefinition]:
        return getattr(fn, "__temporal_query_definition", None)

    def __post_init__(self) -> None:
        if self.arg_types is None and self.ret_type is None:
            arg_types, ret_type = temporalio.common._type_hints_from_func(self.fn)
            # If dynamic, assert it
            if not self.name:
                object.__setattr__(
                    self,
                    "dynamic_vararg",
                    not _assert_dynamic_handler_args(
                        self.fn, arg_types, self.is_method
                    ),
                )
            object.__setattr__(self, "arg_types", arg_types)
            object.__setattr__(self, "ret_type", ret_type)

    def bind_fn(self, obj: Any) -> Callable[..., Any]:
        return _bind_method(obj, self.fn)


@dataclass(frozen=True)
class _UpdateDefinition:
    # None if dynamic
    name: Optional[str]
    fn: Callable[..., Union[Any, Awaitable[Any]]]
    is_method: bool
    # Types loaded on post init if None
    arg_types: Optional[List[Type]] = None
    ret_type: Optional[Type] = None
    validator: Optional[Callable[..., None]] = None
    dynamic_vararg: bool = False

    def __post_init__(self) -> None:
        if self.arg_types is None:
            arg_types, ret_type = temporalio.common._type_hints_from_func(self.fn)
            # Disallow dynamic varargs
            if not self.name and not _assert_dynamic_handler_args(
                self.fn, arg_types, self.is_method
            ):
                raise RuntimeError(
                    "Dynamic updates do not support a vararg third param, use Sequence[RawValue]",
                )
            object.__setattr__(self, "arg_types", arg_types)
            object.__setattr__(self, "ret_type", ret_type)

    def bind_fn(self, obj: Any) -> Callable[..., Any]:
        return _bind_method(obj, self.fn)

    def bind_validator(self, obj: Any) -> Callable[..., Any]:
        if self.validator is not None:
            return _bind_method(obj, self.validator)
        return lambda *args, **kwargs: None

    def set_validator(self, validator: Callable[..., None]) -> None:
        if self.validator:
            raise RuntimeError(f"Validator already set for update {self.name}")
        object.__setattr__(self, "validator", validator)


# See https://mypy.readthedocs.io/en/latest/runtime_troubles.html#using-classes-that-are-generic-in-stubs-but-not-at-runtime
if TYPE_CHECKING:

    class _AsyncioTask(asyncio.Task[AnyType]):
        pass

else:

    class _AsyncioTask(Generic[AnyType], asyncio.Task):
        pass


class ActivityHandle(_AsyncioTask[ReturnType]):  # type: ignore[type-var]
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

    task_queue: Optional[str]
    schedule_to_close_timeout: Optional[timedelta]
    schedule_to_start_timeout: Optional[timedelta]
    start_to_close_timeout: Optional[timedelta]
    heartbeat_timeout: Optional[timedelta]
    retry_policy: Optional[temporalio.common.RetryPolicy]
    cancellation_type: ActivityCancellationType
    activity_id: Optional[str]
    versioning_intent: Optional[VersioningIntent]


# Overload for async no-param activity
@overload
def start_activity(
    activity: CallableAsyncNoParam[ReturnType],
    *,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for sync no-param activity
@overload
def start_activity(
    activity: CallableSyncNoParam[ReturnType],
    *,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for async single-param activity
@overload
def start_activity(
    activity: CallableAsyncSingleParam[ParamType, ReturnType],
    arg: ParamType,
    *,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for sync single-param activity
@overload
def start_activity(
    activity: CallableSyncSingleParam[ParamType, ReturnType],
    arg: ParamType,
    *,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for async multi-param activity
@overload
def start_activity(
    activity: Callable[..., Awaitable[ReturnType]],
    *,
    args: Sequence[Any],
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for sync multi-param activity
@overload
def start_activity(
    activity: Callable[..., ReturnType],
    *,
    args: Sequence[Any],
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for string-name activity
@overload
def start_activity(
    activity: str,
    arg: Any = temporalio.common._arg_unset,
    *,
    args: Sequence[Any] = [],
    task_queue: Optional[str] = None,
    result_type: Optional[Type] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ActivityHandle[Any]:
    ...


def start_activity(
    activity: Any,
    arg: Any = temporalio.common._arg_unset,
    *,
    args: Sequence[Any] = [],
    task_queue: Optional[str] = None,
    result_type: Optional[Type] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ActivityHandle[Any]:
    """Start an activity and return its handle.

    At least one of ``schedule_to_close_timeout`` or ``start_to_close_timeout``
    must be present.

    Args:
        activity: Activity name or function reference.
        arg: Single argument to the activity.
        args: Multiple arguments to the activity. Cannot be set if arg is.
        task_queue: Task queue to run the activity on. Defaults to the current
            workflow's task queue.
        result_type: For string activities, this can set the specific result
            type hint to deserialize into.
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
        activity_id: Optional unique identifier for the activity. This is an
            advanced setting that should not be set unless users are sure they
            need to. Contact Temporal before setting this value.
        versioning_intent: When using the Worker Versioning feature, specifies whether this Activity
            should run on a worker with a compatible Build Id or not.

    Returns:
        An activity handle to the activity which is an async task.
    """
    return _Runtime.current().workflow_start_activity(
        activity,
        *temporalio.common._arg_or_args(arg, args),
        task_queue=task_queue,
        result_type=result_type,
        schedule_to_close_timeout=schedule_to_close_timeout,
        schedule_to_start_timeout=schedule_to_start_timeout,
        start_to_close_timeout=start_to_close_timeout,
        heartbeat_timeout=heartbeat_timeout,
        retry_policy=retry_policy,
        cancellation_type=cancellation_type,
        activity_id=activity_id,
        versioning_intent=versioning_intent,
    )


# Overload for async no-param activity
@overload
async def execute_activity(
    activity: CallableAsyncNoParam[ReturnType],
    *,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ReturnType:
    ...


# Overload for sync no-param activity
@overload
async def execute_activity(
    activity: CallableSyncNoParam[ReturnType],
    *,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ReturnType:
    ...


# Overload for async single-param activity
@overload
async def execute_activity(
    activity: CallableAsyncSingleParam[ParamType, ReturnType],
    arg: ParamType,
    *,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ReturnType:
    ...


# Overload for sync single-param activity
@overload
async def execute_activity(
    activity: CallableSyncSingleParam[ParamType, ReturnType],
    arg: ParamType,
    *,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ReturnType:
    ...


# Overload for async multi-param activity
@overload
async def execute_activity(
    activity: Callable[..., Awaitable[ReturnType]],
    *,
    args: Sequence[Any],
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ReturnType:
    ...


# Overload for sync multi-param activity
@overload
async def execute_activity(
    activity: Callable[..., ReturnType],
    *,
    args: Sequence[Any],
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ReturnType:
    ...


# Overload for string-name activity
@overload
async def execute_activity(
    activity: str,
    arg: Any = temporalio.common._arg_unset,
    *,
    args: Sequence[Any] = [],
    task_queue: Optional[str] = None,
    result_type: Optional[Type] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> Any:
    ...


async def execute_activity(
    activity: Any,
    arg: Any = temporalio.common._arg_unset,
    *,
    args: Sequence[Any] = [],
    task_queue: Optional[str] = None,
    result_type: Optional[Type] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> Any:
    """Start an activity and wait for completion.

    This is a shortcut for ``await`` :py:meth:`start_activity`.
    """
    # We call the runtime directly instead of top-level start_activity to ensure
    # we don't miss new parameters
    return await _Runtime.current().workflow_start_activity(
        activity,
        *temporalio.common._arg_or_args(arg, args),
        task_queue=task_queue,
        result_type=result_type,
        schedule_to_close_timeout=schedule_to_close_timeout,
        schedule_to_start_timeout=schedule_to_start_timeout,
        start_to_close_timeout=start_to_close_timeout,
        heartbeat_timeout=heartbeat_timeout,
        retry_policy=retry_policy,
        cancellation_type=cancellation_type,
        activity_id=activity_id,
        versioning_intent=versioning_intent,
    )


# Overload for async no-param activity
@overload
def start_activity_class(
    activity: Type[CallableAsyncNoParam[ReturnType]],
    *,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for sync no-param activity
@overload
def start_activity_class(
    activity: Type[CallableSyncNoParam[ReturnType]],
    *,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for async single-param activity
@overload
def start_activity_class(
    activity: Type[CallableAsyncSingleParam[ParamType, ReturnType]],
    arg: ParamType,
    *,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for sync single-param activity
@overload
def start_activity_class(
    activity: Type[CallableSyncSingleParam[ParamType, ReturnType]],
    arg: ParamType,
    *,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for async multi-param activity
@overload
def start_activity_class(
    activity: Type[Callable[..., Awaitable[ReturnType]]],
    *,
    args: Sequence[Any],
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for sync multi-param activity
@overload
def start_activity_class(
    activity: Type[Callable[..., ReturnType]],
    *,
    args: Sequence[Any],
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ActivityHandle[ReturnType]:
    ...


def start_activity_class(
    activity: Type[Callable],
    arg: Any = temporalio.common._arg_unset,
    *,
    args: Sequence[Any] = [],
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ActivityHandle[Any]:
    """Start an activity from a callable class.

    See :py:meth:`start_activity` for parameter and return details.
    """
    return _Runtime.current().workflow_start_activity(
        activity,
        *temporalio.common._arg_or_args(arg, args),
        task_queue=task_queue,
        result_type=None,
        schedule_to_close_timeout=schedule_to_close_timeout,
        schedule_to_start_timeout=schedule_to_start_timeout,
        start_to_close_timeout=start_to_close_timeout,
        heartbeat_timeout=heartbeat_timeout,
        retry_policy=retry_policy,
        cancellation_type=cancellation_type,
        activity_id=activity_id,
        versioning_intent=versioning_intent,
    )


# Overload for async no-param activity
@overload
async def execute_activity_class(
    activity: Type[CallableAsyncNoParam[ReturnType]],
    *,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ReturnType:
    ...


# Overload for sync no-param activity
@overload
async def execute_activity_class(
    activity: Type[CallableSyncNoParam[ReturnType]],
    *,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ReturnType:
    ...


# Overload for async single-param activity
@overload
async def execute_activity_class(
    activity: Type[CallableAsyncSingleParam[ParamType, ReturnType]],
    arg: ParamType,
    *,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ReturnType:
    ...


# Overload for sync single-param activity
@overload
async def execute_activity_class(
    activity: Type[CallableSyncSingleParam[ParamType, ReturnType]],
    arg: ParamType,
    *,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ReturnType:
    ...


# Overload for async multi-param activity
@overload
async def execute_activity_class(
    activity: Type[Callable[..., Awaitable[ReturnType]]],
    *,
    args: Sequence[Any],
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ReturnType:
    ...


# Overload for sync multi-param activity
@overload
async def execute_activity_class(
    activity: Type[Callable[..., ReturnType]],
    *,
    args: Sequence[Any],
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ReturnType:
    ...


async def execute_activity_class(
    activity: Type[Callable],
    arg: Any = temporalio.common._arg_unset,
    *,
    args: Sequence[Any] = [],
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> Any:
    """Start an activity from a callable class and wait for completion.

    This is a shortcut for ``await`` :py:meth:`start_activity_class`.
    """
    return await _Runtime.current().workflow_start_activity(
        activity,
        *temporalio.common._arg_or_args(arg, args),
        task_queue=task_queue,
        result_type=None,
        schedule_to_close_timeout=schedule_to_close_timeout,
        schedule_to_start_timeout=schedule_to_start_timeout,
        start_to_close_timeout=start_to_close_timeout,
        heartbeat_timeout=heartbeat_timeout,
        retry_policy=retry_policy,
        cancellation_type=cancellation_type,
        activity_id=activity_id,
        versioning_intent=versioning_intent,
    )


# Overload for async no-param activity
@overload
def start_activity_method(
    activity: MethodAsyncNoParam[SelfType, ReturnType],
    *,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for sync no-param activity
@overload
def start_activity_method(
    activity: MethodSyncNoParam[SelfType, ReturnType],
    *,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for async single-param activity
@overload
def start_activity_method(
    activity: MethodAsyncSingleParam[SelfType, ParamType, ReturnType],
    arg: ParamType,
    *,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for sync single-param activity
@overload
def start_activity_method(
    activity: MethodSyncSingleParam[SelfType, ParamType, ReturnType],
    arg: ParamType,
    *,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for async multi-param activity
@overload
def start_activity_method(
    activity: Callable[Concatenate[SelfType, MultiParamSpec], Awaitable[ReturnType]],
    *,
    args: Sequence[Any],
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for sync multi-param activity
@overload
def start_activity_method(
    activity: Callable[Concatenate[SelfType, MultiParamSpec], ReturnType],
    *,
    args: Sequence[Any],
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ActivityHandle[ReturnType]:
    ...


def start_activity_method(
    activity: Callable,
    arg: Any = temporalio.common._arg_unset,
    *,
    args: Sequence[Any] = [],
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ActivityHandle[Any]:
    """Start an activity from a method.

    See :py:meth:`start_activity` for parameter and return details.
    """
    return _Runtime.current().workflow_start_activity(
        activity,
        *temporalio.common._arg_or_args(arg, args),
        task_queue=task_queue,
        result_type=None,
        schedule_to_close_timeout=schedule_to_close_timeout,
        schedule_to_start_timeout=schedule_to_start_timeout,
        start_to_close_timeout=start_to_close_timeout,
        heartbeat_timeout=heartbeat_timeout,
        retry_policy=retry_policy,
        cancellation_type=cancellation_type,
        activity_id=activity_id,
        versioning_intent=versioning_intent,
    )


# Overload for async no-param activity
@overload
async def execute_activity_method(
    activity: MethodAsyncNoParam[SelfType, ReturnType],
    *,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ReturnType:
    ...


# Overload for sync no-param activity
@overload
async def execute_activity_method(
    activity: MethodSyncNoParam[SelfType, ReturnType],
    *,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ReturnType:
    ...


# Overload for async single-param activity
@overload
async def execute_activity_method(
    activity: MethodAsyncSingleParam[SelfType, ParamType, ReturnType],
    arg: ParamType,
    *,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ReturnType:
    ...


# Overload for sync single-param activity
@overload
async def execute_activity_method(
    activity: MethodSyncSingleParam[SelfType, ParamType, ReturnType],
    arg: ParamType,
    *,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ReturnType:
    ...


# Overload for async multi-param activity
@overload
async def execute_activity_method(
    activity: Callable[Concatenate[SelfType, MultiParamSpec], Awaitable[ReturnType]],
    *,
    args: Sequence[Any],
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ReturnType:
    ...


# Overload for sync multi-param activity
@overload
async def execute_activity_method(
    activity: Callable[Concatenate[SelfType, MultiParamSpec], ReturnType],
    *,
    args: Sequence[Any],
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ReturnType:
    ...


async def execute_activity_method(
    activity: Callable,
    arg: Any = temporalio.common._arg_unset,
    *,
    args: Sequence[Any] = [],
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> Any:
    """Start an activity from a method and wait for completion.

    This is a shortcut for ``await`` :py:meth:`start_activity_method`.
    """
    # We call the runtime directly instead of top-level start_activity to ensure
    # we don't miss new parameters
    return await _Runtime.current().workflow_start_activity(
        activity,
        *temporalio.common._arg_or_args(arg, args),
        task_queue=task_queue,
        result_type=None,
        schedule_to_close_timeout=schedule_to_close_timeout,
        schedule_to_start_timeout=schedule_to_start_timeout,
        start_to_close_timeout=start_to_close_timeout,
        heartbeat_timeout=heartbeat_timeout,
        retry_policy=retry_policy,
        cancellation_type=cancellation_type,
        activity_id=activity_id,
        versioning_intent=versioning_intent,
    )


class LocalActivityConfig(TypedDict, total=False):
    """TypedDict of config that can be used for :py:func:`start_local_activity`
    and :py:func:`execute_local_activity`.
    """

    schedule_to_close_timeout: Optional[timedelta]
    schedule_to_start_timeout: Optional[timedelta]
    start_to_close_timeout: Optional[timedelta]
    retry_policy: Optional[temporalio.common.RetryPolicy]
    local_retry_threshold: Optional[timedelta]
    cancellation_type: ActivityCancellationType
    activity_id: Optional[str]


# Overload for async no-param activity
@overload
def start_local_activity(
    activity: CallableAsyncNoParam[ReturnType],
    *,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for sync no-param activity
@overload
def start_local_activity(
    activity: CallableSyncNoParam[ReturnType],
    *,
    activity_id: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for async single-param activity
@overload
def start_local_activity(
    activity: CallableAsyncSingleParam[ParamType, ReturnType],
    arg: ParamType,
    *,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for sync single-param activity
@overload
def start_local_activity(
    activity: CallableSyncSingleParam[ParamType, ReturnType],
    arg: ParamType,
    *,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for async multi-param activity
@overload
def start_local_activity(
    activity: Callable[..., Awaitable[ReturnType]],
    *,
    args: Sequence[Any],
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for sync multi-param activity
@overload
def start_local_activity(
    activity: Callable[..., ReturnType],
    *,
    args: Sequence[Any],
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for string-name activity
@overload
def start_local_activity(
    activity: str,
    arg: Any = temporalio.common._arg_unset,
    *,
    args: Sequence[Any] = [],
    result_type: Optional[Type] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ActivityHandle[Any]:
    ...


def start_local_activity(
    activity: Any,
    arg: Any = temporalio.common._arg_unset,
    *,
    args: Sequence[Any] = [],
    result_type: Optional[Type] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ActivityHandle[Any]:
    """Start a local activity and return its handle.

    At least one of ``schedule_to_close_timeout`` or ``start_to_close_timeout``
    must be present.

    .. warning::
        Local activities are currently experimental.

    Args:
        activity: Activity name or function reference.
        arg: Single argument to the activity.
        args: Multiple arguments to the activity. Cannot be set if arg is.
        result_type: For string activities, this can set the specific result
            type hint to deserialize into.
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
        activity_id: Optional unique identifier for the activity. This is an
            advanced setting that should not be set unless users are sure they
            need to. Contact Temporal before setting this value.

    Returns:
        An activity handle to the activity which is an async task.
    """
    return _Runtime.current().workflow_start_local_activity(
        activity,
        *temporalio.common._arg_or_args(arg, args),
        result_type=result_type,
        schedule_to_close_timeout=schedule_to_close_timeout,
        schedule_to_start_timeout=schedule_to_start_timeout,
        start_to_close_timeout=start_to_close_timeout,
        retry_policy=retry_policy,
        local_retry_threshold=local_retry_threshold,
        cancellation_type=cancellation_type,
        activity_id=activity_id,
    )


# Overload for async no-param activity
@overload
async def execute_local_activity(
    activity: CallableAsyncNoParam[ReturnType],
    *,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ReturnType:
    ...


# Overload for sync no-param activity
@overload
async def execute_local_activity(
    activity: CallableSyncNoParam[ReturnType],
    *,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ReturnType:
    ...


# Overload for async single-param activity
@overload
async def execute_local_activity(
    activity: CallableAsyncSingleParam[ParamType, ReturnType],
    arg: ParamType,
    *,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ReturnType:
    ...


# Overload for sync single-param activity
@overload
async def execute_local_activity(
    activity: CallableSyncSingleParam[ParamType, ReturnType],
    arg: ParamType,
    *,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ReturnType:
    ...


# Overload for async multi-param activity
@overload
async def execute_local_activity(
    activity: Callable[..., Awaitable[ReturnType]],
    *,
    args: Sequence[Any],
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ReturnType:
    ...


# Overload for sync multi-param activity
@overload
async def execute_local_activity(
    activity: Callable[..., ReturnType],
    *,
    args: Sequence[Any],
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ReturnType:
    ...


# Overload for string-name activity
@overload
async def execute_local_activity(
    activity: str,
    arg: Any = temporalio.common._arg_unset,
    *,
    args: Sequence[Any] = [],
    result_type: Optional[Type] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> Any:
    ...


async def execute_local_activity(
    activity: Any,
    arg: Any = temporalio.common._arg_unset,
    *,
    args: Sequence[Any] = [],
    result_type: Optional[Type] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> Any:
    """Start a local activity and wait for completion.

    This is a shortcut for ``await`` :py:meth:`start_local_activity`.

    .. warning::
        Local activities are currently experimental.
    """
    # We call the runtime directly instead of top-level start_local_activity to
    # ensure we don't miss new parameters
    return await _Runtime.current().workflow_start_local_activity(
        activity,
        *temporalio.common._arg_or_args(arg, args),
        result_type=result_type,
        schedule_to_close_timeout=schedule_to_close_timeout,
        schedule_to_start_timeout=schedule_to_start_timeout,
        start_to_close_timeout=start_to_close_timeout,
        retry_policy=retry_policy,
        local_retry_threshold=local_retry_threshold,
        cancellation_type=cancellation_type,
        activity_id=activity_id,
    )


# Overload for async no-param activity
@overload
def start_local_activity_class(
    activity: Type[CallableAsyncNoParam[ReturnType]],
    *,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for sync no-param activity
@overload
def start_local_activity_class(
    activity: Type[CallableSyncNoParam[ReturnType]],
    *,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for async single-param activity
@overload
def start_local_activity_class(
    activity: Type[CallableAsyncSingleParam[ParamType, ReturnType]],
    arg: ParamType,
    *,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for sync single-param activity
@overload
def start_local_activity_class(
    activity: Type[CallableSyncSingleParam[ParamType, ReturnType]],
    arg: ParamType,
    *,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for async multi-param activity
@overload
def start_local_activity_class(
    activity: Type[Callable[..., Awaitable[ReturnType]]],
    *,
    args: Sequence[Any],
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for sync multi-param activity
@overload
def start_local_activity_class(
    activity: Type[Callable[..., ReturnType]],
    *,
    args: Sequence[Any],
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ActivityHandle[ReturnType]:
    ...


def start_local_activity_class(
    activity: Type[Callable],
    arg: Any = temporalio.common._arg_unset,
    *,
    args: Sequence[Any] = [],
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ActivityHandle[Any]:
    """Start a local activity from a callable class.

    See :py:meth:`start_local_activity` for parameter and return details.

    .. warning::
        Local activities are currently experimental.
    """
    return _Runtime.current().workflow_start_local_activity(
        activity,
        *temporalio.common._arg_or_args(arg, args),
        result_type=None,
        schedule_to_close_timeout=schedule_to_close_timeout,
        schedule_to_start_timeout=schedule_to_start_timeout,
        start_to_close_timeout=start_to_close_timeout,
        retry_policy=retry_policy,
        local_retry_threshold=local_retry_threshold,
        cancellation_type=cancellation_type,
        activity_id=activity_id,
    )


# Overload for async no-param activity
@overload
async def execute_local_activity_class(
    activity: Type[CallableAsyncNoParam[ReturnType]],
    *,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ReturnType:
    ...


# Overload for sync no-param activity
@overload
async def execute_local_activity_class(
    activity: Type[CallableSyncNoParam[ReturnType]],
    *,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ReturnType:
    ...


# Overload for async single-param activity
@overload
async def execute_local_activity_class(
    activity: Type[CallableAsyncSingleParam[ParamType, ReturnType]],
    arg: ParamType,
    *,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ReturnType:
    ...


# Overload for sync single-param activity
@overload
async def execute_local_activity_class(
    activity: Type[CallableSyncSingleParam[ParamType, ReturnType]],
    arg: ParamType,
    *,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ReturnType:
    ...


# Overload for async multi-param activity
@overload
async def execute_local_activity_class(
    activity: Type[Callable[..., Awaitable[ReturnType]]],
    *,
    args: Sequence[Any],
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ReturnType:
    ...


# Overload for sync multi-param activity
@overload
async def execute_local_activity_class(
    activity: Type[Callable[..., ReturnType]],
    *,
    args: Sequence[Any],
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ReturnType:
    ...


async def execute_local_activity_class(
    activity: Type[Callable],
    arg: Any = temporalio.common._arg_unset,
    *,
    args: Sequence[Any] = [],
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> Any:
    """Start a local activity from a callable class and wait for completion.

    This is a shortcut for ``await`` :py:meth:`start_local_activity_class`.

    .. warning::
        Local activities are currently experimental.
    """
    # We call the runtime directly instead of top-level start_local_activity to
    # ensure we don't miss new parameters
    return await _Runtime.current().workflow_start_local_activity(
        activity,
        *temporalio.common._arg_or_args(arg, args),
        result_type=None,
        schedule_to_close_timeout=schedule_to_close_timeout,
        schedule_to_start_timeout=schedule_to_start_timeout,
        start_to_close_timeout=start_to_close_timeout,
        retry_policy=retry_policy,
        local_retry_threshold=local_retry_threshold,
        cancellation_type=cancellation_type,
        activity_id=activity_id,
    )


# Overload for async no-param activity
@overload
def start_local_activity_method(
    activity: MethodAsyncNoParam[SelfType, ReturnType],
    *,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for sync no-param activity
@overload
def start_local_activity_method(
    activity: MethodSyncNoParam[SelfType, ReturnType],
    *,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for async single-param activity
@overload
def start_local_activity_method(
    activity: MethodAsyncSingleParam[SelfType, ParamType, ReturnType],
    arg: ParamType,
    *,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for sync single-param activity
@overload
def start_local_activity_method(
    activity: MethodSyncSingleParam[SelfType, ParamType, ReturnType],
    arg: ParamType,
    *,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for async multi-param activity
@overload
def start_local_activity_method(
    activity: Callable[Concatenate[SelfType, MultiParamSpec], Awaitable[ReturnType]],
    *,
    args: Sequence[Any],
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ActivityHandle[ReturnType]:
    ...


# Overload for sync multi-param activity
@overload
def start_local_activity_method(
    activity: Callable[Concatenate[SelfType, MultiParamSpec], ReturnType],
    *,
    args: Sequence[Any],
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ActivityHandle[ReturnType]:
    ...


def start_local_activity_method(
    activity: Callable,
    arg: Any = temporalio.common._arg_unset,
    *,
    args: Sequence[Any] = [],
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ActivityHandle[Any]:
    """Start a local activity from a method.

    See :py:meth:`start_local_activity` for parameter and return details.

    .. warning::
        Local activities are currently experimental.
    """
    return _Runtime.current().workflow_start_local_activity(
        activity,
        *temporalio.common._arg_or_args(arg, args),
        result_type=None,
        schedule_to_close_timeout=schedule_to_close_timeout,
        schedule_to_start_timeout=schedule_to_start_timeout,
        start_to_close_timeout=start_to_close_timeout,
        retry_policy=retry_policy,
        local_retry_threshold=local_retry_threshold,
        cancellation_type=cancellation_type,
        activity_id=activity_id,
    )


# Overload for async no-param activity
@overload
async def execute_local_activity_method(
    activity: MethodAsyncNoParam[SelfType, ReturnType],
    *,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ReturnType:
    ...


# Overload for sync no-param activity
@overload
async def execute_local_activity_method(
    activity: MethodSyncNoParam[SelfType, ReturnType],
    *,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ReturnType:
    ...


# Overload for async single-param activity
@overload
async def execute_local_activity_method(
    activity: MethodAsyncSingleParam[SelfType, ParamType, ReturnType],
    arg: ParamType,
    *,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ReturnType:
    ...


# Overload for sync single-param activity
@overload
async def execute_local_activity_method(
    activity: MethodSyncSingleParam[SelfType, ParamType, ReturnType],
    arg: ParamType,
    *,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ReturnType:
    ...


# Overload for async multi-param activity
@overload
async def execute_local_activity_method(
    activity: Callable[Concatenate[SelfType, MultiParamSpec], Awaitable[ReturnType]],
    *,
    args: Sequence[Any],
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ReturnType:
    ...


# Overload for sync multi-param activity
@overload
async def execute_local_activity_method(
    activity: Callable[Concatenate[SelfType, MultiParamSpec], ReturnType],
    *,
    args: Sequence[Any],
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> ReturnType:
    ...


async def execute_local_activity_method(
    activity: Callable,
    arg: Any = temporalio.common._arg_unset,
    *,
    args: Sequence[Any] = [],
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    local_retry_threshold: Optional[timedelta] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
) -> Any:
    """Start a local activity from a method and wait for completion.

    This is a shortcut for ``await`` :py:meth:`start_local_activity_method`.

    .. warning::
        Local activities are currently experimental.
    """
    # We call the runtime directly instead of top-level start_local_activity to
    # ensure we don't miss new parameters
    return await _Runtime.current().workflow_start_local_activity(
        activity,
        *temporalio.common._arg_or_args(arg, args),
        result_type=None,
        schedule_to_close_timeout=schedule_to_close_timeout,
        schedule_to_start_timeout=schedule_to_start_timeout,
        start_to_close_timeout=start_to_close_timeout,
        retry_policy=retry_policy,
        local_retry_threshold=local_retry_threshold,
        cancellation_type=cancellation_type,
        activity_id=activity_id,
    )


class ChildWorkflowHandle(_AsyncioTask[ReturnType], Generic[SelfType, ReturnType]):  # type: ignore[type-var]
    """Handle for interacting with a child workflow.

    This is created via :py:func:`start_child_workflow`.

    This extends :py:class:`asyncio.Task` and supports all task features.
    """

    @property
    def id(self) -> str:
        """ID for the workflow."""
        raise NotImplementedError

    @property
    def first_execution_run_id(self) -> Optional[str]:
        """Run ID for the workflow."""
        raise NotImplementedError

    @overload
    async def signal(
        self,
        signal: MethodSyncOrAsyncNoParam[SelfType, None],
    ) -> None:
        ...

    @overload
    async def signal(
        self,
        signal: MethodSyncOrAsyncSingleParam[SelfType, ParamType, None],
        arg: ParamType,
    ) -> None:
        ...

    @overload
    async def signal(
        self,
        signal: Callable[
            Concatenate[SelfType, MultiParamSpec], Union[Awaitable[None], None]
        ],
        *,
        args: Sequence[Any],
    ) -> None:
        ...

    @overload
    async def signal(
        self,
        signal: str,
        arg: Any = temporalio.common._arg_unset,
        *,
        args: Sequence[Any] = [],
    ) -> None:
        ...

    async def signal(
        self,
        signal: Union[str, Callable],
        arg: Any = temporalio.common._arg_unset,
        *,
        args: Sequence[Any] = [],
    ) -> None:
        """Signal this child workflow.

        Args:
            signal: Name or method reference for the signal.
            arg: Single argument to the signal.
            args: Multiple arguments to the signal. Cannot be set if arg is.

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

    id: Optional[str]
    task_queue: Optional[str]
    cancellation_type: ChildWorkflowCancellationType
    parent_close_policy: ParentClosePolicy
    execution_timeout: Optional[timedelta]
    run_timeout: Optional[timedelta]
    task_timeout: Optional[timedelta]
    id_reuse_policy: temporalio.common.WorkflowIDReusePolicy
    retry_policy: Optional[temporalio.common.RetryPolicy]
    cron_schedule: str
    memo: Optional[Mapping[str, Any]]
    search_attributes: Optional[
        Union[
            temporalio.common.SearchAttributes, temporalio.common.TypedSearchAttributes
        ]
    ]


# Overload for no-param workflow
@overload
async def start_child_workflow(
    workflow: MethodAsyncNoParam[SelfType, ReturnType],
    *,
    id: Optional[str] = None,
    task_queue: Optional[str] = None,
    cancellation_type: ChildWorkflowCancellationType = ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
    parent_close_policy: ParentClosePolicy = ParentClosePolicy.TERMINATE,
    execution_timeout: Optional[timedelta] = None,
    run_timeout: Optional[timedelta] = None,
    task_timeout: Optional[timedelta] = None,
    id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cron_schedule: str = "",
    memo: Optional[Mapping[str, Any]] = None,
    search_attributes: Optional[
        Union[
            temporalio.common.SearchAttributes, temporalio.common.TypedSearchAttributes
        ]
    ] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ChildWorkflowHandle[SelfType, ReturnType]:
    ...


# Overload for single-param workflow
@overload
async def start_child_workflow(
    workflow: MethodAsyncSingleParam[SelfType, ParamType, ReturnType],
    arg: ParamType,
    *,
    id: Optional[str] = None,
    task_queue: Optional[str] = None,
    cancellation_type: ChildWorkflowCancellationType = ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
    parent_close_policy: ParentClosePolicy = ParentClosePolicy.TERMINATE,
    execution_timeout: Optional[timedelta] = None,
    run_timeout: Optional[timedelta] = None,
    task_timeout: Optional[timedelta] = None,
    id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cron_schedule: str = "",
    memo: Optional[Mapping[str, Any]] = None,
    search_attributes: Optional[
        Union[
            temporalio.common.SearchAttributes, temporalio.common.TypedSearchAttributes
        ]
    ] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ChildWorkflowHandle[SelfType, ReturnType]:
    ...


# Overload for multi-param workflow
@overload
async def start_child_workflow(
    workflow: Callable[Concatenate[SelfType, MultiParamSpec], Awaitable[ReturnType]],
    *,
    args: Sequence[Any],
    id: Optional[str] = None,
    task_queue: Optional[str] = None,
    cancellation_type: ChildWorkflowCancellationType = ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
    parent_close_policy: ParentClosePolicy = ParentClosePolicy.TERMINATE,
    execution_timeout: Optional[timedelta] = None,
    run_timeout: Optional[timedelta] = None,
    task_timeout: Optional[timedelta] = None,
    id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cron_schedule: str = "",
    memo: Optional[Mapping[str, Any]] = None,
    search_attributes: Optional[
        Union[
            temporalio.common.SearchAttributes, temporalio.common.TypedSearchAttributes
        ]
    ] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ChildWorkflowHandle[SelfType, ReturnType]:
    ...


# Overload for string-name workflow
@overload
async def start_child_workflow(
    workflow: str,
    arg: Any = temporalio.common._arg_unset,
    *,
    args: Sequence[Any] = [],
    id: Optional[str] = None,
    task_queue: Optional[str] = None,
    result_type: Optional[Type] = None,
    cancellation_type: ChildWorkflowCancellationType = ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
    parent_close_policy: ParentClosePolicy = ParentClosePolicy.TERMINATE,
    execution_timeout: Optional[timedelta] = None,
    run_timeout: Optional[timedelta] = None,
    task_timeout: Optional[timedelta] = None,
    id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cron_schedule: str = "",
    memo: Optional[Mapping[str, Any]] = None,
    search_attributes: Optional[
        Union[
            temporalio.common.SearchAttributes, temporalio.common.TypedSearchAttributes
        ]
    ] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ChildWorkflowHandle[Any, Any]:
    ...


async def start_child_workflow(
    workflow: Any,
    arg: Any = temporalio.common._arg_unset,
    *,
    args: Sequence[Any] = [],
    id: Optional[str] = None,
    task_queue: Optional[str] = None,
    result_type: Optional[Type] = None,
    cancellation_type: ChildWorkflowCancellationType = ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
    parent_close_policy: ParentClosePolicy = ParentClosePolicy.TERMINATE,
    execution_timeout: Optional[timedelta] = None,
    run_timeout: Optional[timedelta] = None,
    task_timeout: Optional[timedelta] = None,
    id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cron_schedule: str = "",
    memo: Optional[Mapping[str, Any]] = None,
    search_attributes: Optional[
        Union[
            temporalio.common.SearchAttributes, temporalio.common.TypedSearchAttributes
        ]
    ] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ChildWorkflowHandle[Any, Any]:
    """Start a child workflow and return its handle.

    Args:
        workflow: String name or class method decorated with ``@workflow.run``
            for the workflow to start.
        arg: Single argument to the child workflow.
        args: Multiple arguments to the child workflow. Cannot be set if arg is.
        id: Optional unique identifier for the workflow execution. If not set,
            defaults to :py:func:`uuid4`.
        task_queue: Task queue to run the workflow on. Defaults to the current
            workflow's task queue.
        result_type: For string workflows, this can set the specific result type
            hint to deserialize into.
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
        search_attributes: Search attributes for the workflow. The dictionary
            form of this is DEPRECATED.
        versioning_intent:  When using the Worker Versioning feature, specifies whether this Child
            Workflow should run on a worker with a compatible Build Id or not.

    Returns:
        A workflow handle to the started/existing workflow.
    """
    temporalio.common._warn_on_deprecated_search_attributes(search_attributes)
    return await _Runtime.current().workflow_start_child_workflow(
        workflow,
        *temporalio.common._arg_or_args(arg, args),
        id=id or str(uuid4()),
        task_queue=task_queue,
        result_type=result_type,
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
        versioning_intent=versioning_intent,
    )


# Overload for no-param workflow
@overload
async def execute_child_workflow(
    workflow: MethodAsyncNoParam[SelfType, ReturnType],
    *,
    id: Optional[str] = None,
    task_queue: Optional[str] = None,
    cancellation_type: ChildWorkflowCancellationType = ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
    parent_close_policy: ParentClosePolicy = ParentClosePolicy.TERMINATE,
    execution_timeout: Optional[timedelta] = None,
    run_timeout: Optional[timedelta] = None,
    task_timeout: Optional[timedelta] = None,
    id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cron_schedule: str = "",
    memo: Optional[Mapping[str, Any]] = None,
    search_attributes: Optional[
        Union[
            temporalio.common.SearchAttributes, temporalio.common.TypedSearchAttributes
        ]
    ] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ReturnType:
    ...


# Overload for single-param workflow
@overload
async def execute_child_workflow(
    workflow: MethodAsyncSingleParam[SelfType, ParamType, ReturnType],
    arg: ParamType,
    *,
    id: Optional[str] = None,
    task_queue: Optional[str] = None,
    cancellation_type: ChildWorkflowCancellationType = ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
    parent_close_policy: ParentClosePolicy = ParentClosePolicy.TERMINATE,
    execution_timeout: Optional[timedelta] = None,
    run_timeout: Optional[timedelta] = None,
    task_timeout: Optional[timedelta] = None,
    id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cron_schedule: str = "",
    memo: Optional[Mapping[str, Any]] = None,
    search_attributes: Optional[
        Union[
            temporalio.common.SearchAttributes, temporalio.common.TypedSearchAttributes
        ]
    ] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ReturnType:
    ...


# Overload for multi-param workflow
@overload
async def execute_child_workflow(
    workflow: Callable[Concatenate[SelfType, MultiParamSpec], Awaitable[ReturnType]],
    *,
    args: Sequence[Any],
    id: Optional[str] = None,
    task_queue: Optional[str] = None,
    cancellation_type: ChildWorkflowCancellationType = ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
    parent_close_policy: ParentClosePolicy = ParentClosePolicy.TERMINATE,
    execution_timeout: Optional[timedelta] = None,
    run_timeout: Optional[timedelta] = None,
    task_timeout: Optional[timedelta] = None,
    id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cron_schedule: str = "",
    memo: Optional[Mapping[str, Any]] = None,
    search_attributes: Optional[
        Union[
            temporalio.common.SearchAttributes, temporalio.common.TypedSearchAttributes
        ]
    ] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> ReturnType:
    ...


# Overload for string-name workflow
@overload
async def execute_child_workflow(
    workflow: str,
    arg: Any = temporalio.common._arg_unset,
    *,
    args: Sequence[Any] = [],
    id: Optional[str] = None,
    task_queue: Optional[str] = None,
    result_type: Optional[Type] = None,
    cancellation_type: ChildWorkflowCancellationType = ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
    parent_close_policy: ParentClosePolicy = ParentClosePolicy.TERMINATE,
    execution_timeout: Optional[timedelta] = None,
    run_timeout: Optional[timedelta] = None,
    task_timeout: Optional[timedelta] = None,
    id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cron_schedule: str = "",
    memo: Optional[Mapping[str, Any]] = None,
    search_attributes: Optional[
        Union[
            temporalio.common.SearchAttributes, temporalio.common.TypedSearchAttributes
        ]
    ] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> Any:
    ...


async def execute_child_workflow(
    workflow: Any,
    arg: Any = temporalio.common._arg_unset,
    *,
    args: Sequence[Any] = [],
    id: Optional[str] = None,
    task_queue: Optional[str] = None,
    result_type: Optional[Type] = None,
    cancellation_type: ChildWorkflowCancellationType = ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
    parent_close_policy: ParentClosePolicy = ParentClosePolicy.TERMINATE,
    execution_timeout: Optional[timedelta] = None,
    run_timeout: Optional[timedelta] = None,
    task_timeout: Optional[timedelta] = None,
    id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cron_schedule: str = "",
    memo: Optional[Mapping[str, Any]] = None,
    search_attributes: Optional[
        Union[
            temporalio.common.SearchAttributes, temporalio.common.TypedSearchAttributes
        ]
    ] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> Any:
    """Start a child workflow and wait for completion.

    This is a shortcut for ``await (await`` :py:meth:`start_child_workflow` ``)``.
    """
    temporalio.common._warn_on_deprecated_search_attributes(search_attributes)
    # We call the runtime directly instead of top-level start_child_workflow to
    # ensure we don't miss new parameters
    handle = await _Runtime.current().workflow_start_child_workflow(
        workflow,
        *temporalio.common._arg_or_args(arg, args),
        id=id or str(uuid4()),
        task_queue=task_queue,
        result_type=result_type,
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
        versioning_intent=versioning_intent,
    )
    return await handle


class ExternalWorkflowHandle(Generic[SelfType]):
    """Handle for interacting with an external workflow.

    This is created via :py:func:`get_external_workflow_handle` or
    :py:func:`get_external_workflow_handle_for`.
    """

    @property
    def id(self) -> str:
        """ID for the workflow."""
        raise NotImplementedError

    @property
    def run_id(self) -> Optional[str]:
        """Run ID for the workflow if any."""
        raise NotImplementedError

    @overload
    async def signal(
        self,
        signal: MethodSyncOrAsyncNoParam[SelfType, None],
    ) -> None:
        ...

    @overload
    async def signal(
        self,
        signal: MethodSyncOrAsyncSingleParam[SelfType, ParamType, None],
        arg: ParamType,
    ) -> None:
        ...

    @overload
    async def signal(
        self,
        signal: str,
        arg: Any = temporalio.common._arg_unset,
        *,
        args: Sequence[Any] = [],
    ) -> None:
        ...

    async def signal(
        self,
        signal: Union[str, Callable],
        arg: Any = temporalio.common._arg_unset,
        *,
        args: Sequence[Any] = [],
    ) -> None:
        """Signal this external workflow.

        Args:
            signal: Name or method reference for the signal.
            arg: Single argument to the signal.
            args: Multiple arguments to the signal. Cannot be set if arg is.

        """
        raise NotImplementedError

    async def cancel(self) -> None:
        """Send a cancellation request to this external workflow.

        This will fail if the workflow cannot accept the request (e.g. if the
        workflow is not found).
        """
        raise NotImplementedError


def get_external_workflow_handle(
    workflow_id: str,
    *,
    run_id: Optional[str] = None,
) -> ExternalWorkflowHandle[Any]:
    """Get a workflow handle to an existing workflow by its ID.

    Args:
        workflow_id: Workflow ID to get a handle to.
        run_id: Optional run ID for the workflow.

    Returns:
        The external workflow handle.
    """
    return _Runtime.current().workflow_get_external_workflow_handle(
        workflow_id, run_id=run_id
    )


def get_external_workflow_handle_for(
    workflow: Union[
        MethodAsyncNoParam[SelfType, Any], MethodAsyncSingleParam[SelfType, Any, Any]
    ],
    workflow_id: str,
    *,
    run_id: Optional[str] = None,
) -> ExternalWorkflowHandle[SelfType]:
    """Get a typed workflow handle to an existing workflow by its ID.

    This is the same as :py:func:`get_external_workflow_handle` but typed. Note,
    the workflow type given is not validated, it is only for typing.

    Args:
        workflow: The workflow run method to use for typing the handle.
        workflow_id: Workflow ID to get a handle to.
        run_id: Optional run ID for the workflow.

    Returns:
        The external workflow handle.
    """
    return get_external_workflow_handle(workflow_id, run_id=run_id)


class ContinueAsNewError(BaseException):
    """Error thrown by :py:func:`continue_as_new`.

    This should not be caught, but instead be allowed to throw out of the
    workflow which then triggers the continue as new. This should never be
    instantiated directly.
    """

    def __init__(self, *args: object) -> None:
        """Direct instantiation is disabled. Use :py:func:`continue_as_new`."""
        if type(self) == ContinueAsNewError:
            raise RuntimeError("Cannot instantiate ContinueAsNewError directly")
        super().__init__(*args)


# Overload for self (unfortunately, cannot type args)
@overload
def continue_as_new(
    arg: Any = temporalio.common._arg_unset,
    *,
    args: Sequence[Any] = [],
    task_queue: Optional[str] = None,
    run_timeout: Optional[timedelta] = None,
    task_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    memo: Optional[Mapping[str, Any]] = None,
    search_attributes: Optional[
        Union[
            temporalio.common.SearchAttributes, temporalio.common.TypedSearchAttributes
        ]
    ] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> NoReturn:
    ...


# Overload for no-param workflow
@overload
def continue_as_new(
    *,
    workflow: MethodAsyncNoParam[SelfType, Any],
    task_queue: Optional[str] = None,
    run_timeout: Optional[timedelta] = None,
    task_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    memo: Optional[Mapping[str, Any]] = None,
    search_attributes: Optional[
        Union[
            temporalio.common.SearchAttributes, temporalio.common.TypedSearchAttributes
        ]
    ] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> NoReturn:
    ...


# Overload for single-param workflow
@overload
def continue_as_new(
    arg: ParamType,
    *,
    workflow: MethodAsyncSingleParam[SelfType, ParamType, Any],
    task_queue: Optional[str] = None,
    run_timeout: Optional[timedelta] = None,
    task_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    memo: Optional[Mapping[str, Any]] = None,
    search_attributes: Optional[
        Union[
            temporalio.common.SearchAttributes, temporalio.common.TypedSearchAttributes
        ]
    ] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> NoReturn:
    ...


# Overload for multi-param workflow
@overload
def continue_as_new(
    *,
    workflow: Callable[Concatenate[SelfType, MultiParamSpec], Awaitable[Any]],
    args: Sequence[Any],
    task_queue: Optional[str] = None,
    run_timeout: Optional[timedelta] = None,
    task_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    memo: Optional[Mapping[str, Any]] = None,
    search_attributes: Optional[
        Union[
            temporalio.common.SearchAttributes, temporalio.common.TypedSearchAttributes
        ]
    ] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> NoReturn:
    ...


# Overload for string-name workflow
@overload
def continue_as_new(
    *,
    workflow: str,
    args: Sequence[Any] = [],
    task_queue: Optional[str] = None,
    run_timeout: Optional[timedelta] = None,
    task_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    memo: Optional[Mapping[str, Any]] = None,
    search_attributes: Optional[
        Union[
            temporalio.common.SearchAttributes, temporalio.common.TypedSearchAttributes
        ]
    ] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> NoReturn:
    ...


def continue_as_new(
    arg: Any = temporalio.common._arg_unset,
    *,
    args: Sequence[Any] = [],
    workflow: Union[None, Callable, str] = None,
    task_queue: Optional[str] = None,
    run_timeout: Optional[timedelta] = None,
    task_timeout: Optional[timedelta] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    memo: Optional[Mapping[str, Any]] = None,
    search_attributes: Optional[
        Union[
            temporalio.common.SearchAttributes, temporalio.common.TypedSearchAttributes
        ]
    ] = None,
    versioning_intent: Optional[VersioningIntent] = None,
) -> NoReturn:
    """Stop the workflow immediately and continue as new.

    Args:
        arg: Single argument to the continued workflow.
        args: Multiple arguments to the continued workflow. Cannot be set if arg
            is.
        workflow: Specific workflow to continue to. Defaults to the current
            workflow.
        task_queue: Task queue to run the workflow on. Defaults to the current
            workflow's task queue.
        run_timeout: Timeout of a single workflow run. Defaults to the current
            workflow's run timeout.
        task_timeout: Timeout of a single workflow task. Defaults to the current
            workflow's task timeout.
        memo: Memo for the workflow. Defaults to the current workflow's memo.
        search_attributes: Search attributes for the workflow. Defaults to the
            current workflow's search attributes. The dictionary form of this is
            DEPRECATED.
        versioning_intent: When using the Worker Versioning feature, specifies whether this Workflow
            should Continue-as-New onto a worker with a compatible Build Id or not.

    Returns:
        Never returns, always raises a :py:class:`ContinueAsNewError`.

    Raises:
        ContinueAsNewError: Always raised by this function. Should not be caught
            but instead be allowed to
    """
    temporalio.common._warn_on_deprecated_search_attributes(search_attributes)
    _Runtime.current().workflow_continue_as_new(
        *temporalio.common._arg_or_args(arg, args),
        workflow=workflow,
        task_queue=task_queue,
        run_timeout=run_timeout,
        task_timeout=task_timeout,
        retry_policy=retry_policy,
        memo=memo,
        search_attributes=search_attributes,
        versioning_intent=versioning_intent,
    )


def get_signal_handler(name: str) -> Optional[Callable]:
    """Get the signal handler for the given name if any.

    This includes handlers created via the ``@workflow.signal`` decorator.

    Args:
        name: Name of the signal.

    Returns:
        Callable for the signal if any. If a handler is not found for the name,
        this will not return the dynamic handler even if there is one.
    """
    return _Runtime.current().workflow_get_signal_handler(name)


def set_signal_handler(name: str, handler: Optional[Callable]) -> None:
    """Set or unset the signal handler for the given name.

    This overrides any existing handlers for the given name, including handlers
    created via the ``@workflow.signal`` decorator.

    When set, all unhandled past signals for the given name are immediately sent
    to the handler.

    Args:
        name: Name of the signal.
        handler: Callable to set or None to unset.
    """
    _Runtime.current().workflow_set_signal_handler(name, handler)


def get_dynamic_signal_handler() -> Optional[Callable]:
    """Get the dynamic signal handler if any.

    This includes dynamic handlers created via the ``@workflow.signal``
    decorator.

    Returns:
        Callable for the dynamic signal handler if any.
    """
    return _Runtime.current().workflow_get_signal_handler(None)


def set_dynamic_signal_handler(handler: Optional[Callable]) -> None:
    """Set or unset the dynamic signal handler.

    This overrides the existing dynamic handler even if it was created via the
    ``@workflow.signal`` decorator.

    When set, all unhandled past signals are immediately sent to the handler.

    Args:
        handler: Callable to set or None to unset.
    """
    _Runtime.current().workflow_set_signal_handler(None, handler)


def get_query_handler(name: str) -> Optional[Callable]:
    """Get the query handler for the given name if any.

    This includes handlers created via the ``@workflow.query`` decorator.

    Args:
        name: Name of the query.

    Returns:
        Callable for the query if any. If a handler is not found for the name,
        this will not return the dynamic handler even if there is one.
    """
    return _Runtime.current().workflow_get_query_handler(name)


def set_query_handler(name: str, handler: Optional[Callable]) -> None:
    """Set or unset the query handler for the given name.

    This overrides any existing handlers for the given name, including handlers
    created via the ``@workflow.query`` decorator.

    Args:
        name: Name of the query.
        handler: Callable to set or None to unset.
    """
    _Runtime.current().workflow_set_query_handler(name, handler)


def get_dynamic_query_handler() -> Optional[Callable]:
    """Get the dynamic query handler if any.

    This includes dynamic handlers created via the ``@workflow.query``
    decorator.

    Returns:
        Callable for the dynamic query handler if any.
    """
    return _Runtime.current().workflow_get_query_handler(None)


def set_dynamic_query_handler(handler: Optional[Callable]) -> None:
    """Set or unset the dynamic query handler.

    This overrides the existing dynamic handler even if it was created via the
    ``@workflow.query`` decorator.

    Args:
        handler: Callable to set or None to unset.
    """
    _Runtime.current().workflow_set_query_handler(None, handler)


def get_update_handler(name: str) -> Optional[Callable]:
    """Get the update handler for the given name if any.

    This includes handlers created via the ``@workflow.update`` decorator.

    Args:
        name: Name of the update.

    Returns:
        Callable for the update if any. If a handler is not found for the name,
        this will not return the dynamic handler even if there is one.
    """
    return _Runtime.current().workflow_get_update_handler(name)


def set_update_handler(
    name: str, handler: Optional[Callable], *, validator: Optional[Callable] = None
) -> None:
    """Set or unset the update handler for the given name.

    This overrides any existing handlers for the given name, including handlers
    created via the ``@workflow.update`` decorator.

    Args:
        name: Name of the update.
        handler: Callable to set or None to unset.
        validator: Callable to set or None to unset as the update validator.
    """
    _Runtime.current().workflow_set_update_handler(name, handler, validator)


def get_dynamic_update_handler() -> Optional[Callable]:
    """Get the dynamic update handler if any.

    This includes dynamic handlers created via the ``@workflow.update``
    decorator.

    Returns:
        Callable for the dynamic update handler if any.
    """
    return _Runtime.current().workflow_get_update_handler(None)


def set_dynamic_update_handler(
    handler: Optional[Callable], *, validator: Optional[Callable] = None
) -> None:
    """Set or unset the dynamic update handler.

    This overrides the existing dynamic handler even if it was created via the
    ``@workflow.update`` decorator.

    Args:
        handler: Callable to set or None to unset.
        validator: Callable to set or None to unset as the update validator.
    """
    _Runtime.current().workflow_set_update_handler(None, handler, validator)


def _is_unbound_method_on_cls(fn: Callable[..., Any], cls: Type) -> bool:
    # Python 3 does not make this easy, ref https://stackoverflow.com/questions/3589311
    return (
        inspect.isfunction(fn)
        and inspect.getmodule(fn) is inspect.getmodule(cls)
        and fn.__qualname__.rsplit(".", 1)[0] == cls.__name__
    )


class _UnexpectedEvictionError(temporalio.exceptions.TemporalError):
    def __init__(
        self,
        reason: temporalio.bridge.proto.workflow_activation.RemoveFromCache.EvictionReason.ValueType,
        message: str,
    ) -> None:
        self.reason = temporalio.bridge.proto.workflow_activation.RemoveFromCache.EvictionReason.Name(
            reason
        )
        self.message = message
        super().__init__(f"{self.reason}: {message}")


class NondeterminismError(temporalio.exceptions.TemporalError):
    """Error that can be thrown during replay for non-deterministic workflow."""

    def __init__(self, message: str) -> None:
        """Initialize a nondeterminism error."""
        super().__init__(message)
        self.message = message


class ReadOnlyContextError(temporalio.exceptions.TemporalError):
    """Error thrown when trying to do mutable workflow calls in a read-only
    context like a query or update validator.
    """

    def __init__(self, message: str) -> None:
        """Initialize a read-only context error."""
        super().__init__(message)
        self.message = message


class _NotInWorkflowEventLoopError(temporalio.exceptions.TemporalError):
    def __init__(self, *args: object) -> None:
        super().__init__("Not in workflow event loop")
        self.message = "Not in workflow event loop"


class VersioningIntent(Enum):
    """Indicates whether the user intends certain commands to be run on a compatible worker Build
    Id version or not.

    `COMPATIBLE` indicates that the command should run on a worker with compatible version if
    possible. It may not be possible if the target task queue does not also have knowledge of the
    current worker's Build Id.

    `DEFAULT` indicates that the command should run on the target task queue's current
    overall-default Build Id.

    Where this type is accepted optionally, an unset value indicates that the SDK should choose the
    most sensible default behavior for the type of command, accounting for whether the command will
    be run on the same task queue as the current worker.
    """

    COMPATIBLE = 1
    DEFAULT = 2

    def _to_proto(self) -> temporalio.bridge.proto.common.VersioningIntent.ValueType:
        if self == VersioningIntent.COMPATIBLE:
            return temporalio.bridge.proto.common.VersioningIntent.COMPATIBLE
        elif self == VersioningIntent.DEFAULT:
            return temporalio.bridge.proto.common.VersioningIntent.DEFAULT
        return temporalio.bridge.proto.common.VersioningIntent.UNSPECIFIED
