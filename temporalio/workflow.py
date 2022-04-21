from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
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

from typing_extensions import Literal, Protocol

WorkflowClass = TypeVar("WorkflowClass", bound=Type)
ActivityReturnType = TypeVar("ActivityReturnType")
T = TypeVar("T")


@overload
def defn(cls: WorkflowClass) -> WorkflowClass:
    ...


@overload
def defn(*, name: str) -> Callable[[WorkflowClass], WorkflowClass]:
    ...


def defn(cls: Optional[WorkflowClass] = None, *, name: Optional[str] = None):
    """Decorator for workflow classes.

    Activities can be async or non-async.

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


# TODO(cretz): Move this to a variable on the event loop?
_current_context: contextvars.ContextVar[_Context] = contextvars.ContextVar("workflow")


class _EventLoopProto(Protocol):
    async def wait_condition(
        self, fn: Callable[[], Awaitable[bool]], *, timeout: Optional[float] = None
    ) -> None:
        ...

    def time(self) -> float:
        ...


def _get_running_loop() -> _EventLoopProto:
    loop = asyncio.get_running_loop()
    if not getattr(loop, "__temporal_workflow_loop", False):
        raise RuntimeError("Not in workflow event loop")
    return cast(_EventLoopProto, loop)


def _mark_as_workflow_loop(loop: _EventLoopProto) -> None:
    setattr(loop, "__temporal_workflow_loop", True)


@dataclass
class _Context:
    info: Callable[[], Info]
    _logger_details: Optional[Mapping[str, Any]] = None

    @staticmethod
    def current() -> _Context:
        context = _current_context.get(None)
        if not context:
            raise RuntimeError("Not in workflow context")
        return context

    @staticmethod
    def set(context: _Context) -> None:
        _current_context.set(context)

    @property
    def logger_details(self) -> Mapping[str, Any]:
        if self._logger_details is None:
            self._logger_details = self.info()._logger_details()
        return self._logger_details


def info() -> Info:
    return _Context.current().info()


def now() -> datetime:
    return datetime.utcfromtimestamp(_get_running_loop().time())


async def wait_condition(
    fn: Callable[[], Awaitable[bool]], *, timeout: Optional[float] = None
) -> None:
    await _get_running_loop().wait_condition(fn, timeout=timeout)


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
            context = _current_context.get(None)
            if context:
                if self.workflow_info_on_message:
                    msg = f"{msg} ({context.logger_details})"
                if self.workflow_info_on_extra:
                    # Extra can be absent or None, this handles both
                    extra = kwargs.get("extra", None) or {}
                    extra["workflow_info"] = context.info()
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
    def from_run_fn(fn: Callable[..., Awaitable[Any]]) -> Optional[_Definition]:
        return getattr(fn, "__temporal_workflow_definition", None)

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


class CancellationScope:
    @property
    @staticmethod
    def current() -> CancellationScope:
        raise NotImplementedError()

    def __init__(
        self,
        *,
        # Default is current
        parent: Optional[CancellationScope] = None,
        detached: bool = False,
        timeout: Optional[timedelta] = None,
    ) -> None:
        raise NotImplementedError()

    async def run(self, fn: Callable[..., Union[Awaitable[T], T]]) -> T:
        raise NotImplementedError()

    def __enter__(self) -> CancellationScope:
        pass

    def __exit__(self) -> None:
        raise NotImplementedError()

    def cancel(self) -> None:
        raise NotImplementedError()

    @property
    def cancelled(self) -> bool:
        raise NotImplementedError()


class ActivityHandle(ABC, Generic[ActivityReturnType]):
    @abstractmethod
    async def result(self) -> ActivityReturnType:
        ...

    @abstractmethod
    async def cancel(self) -> None:
        ...


def _is_unbound_method_on_cls(fn: Callable[..., Any], cls: Type) -> bool:
    # Python 3 does not make this easy, ref https://stackoverflow.com/questions/3589311
    return (
        inspect.isfunction(fn)
        and inspect.getmodule(fn) is inspect.getmodule(cls)
        and fn.__qualname__.rsplit(".", 1)[0] == cls.__name__
    )
