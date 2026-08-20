"""Call-site declaration that a result is consumed as a ValueHandle."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, overload

import temporalio.common

from ..types import (
    CallableAsyncNoParam,
    CallableAsyncSingleParam,
    CallableSyncNoParam,
    CallableSyncSingleParam,
    MethodAsyncNoParam,
    MethodAsyncSingleParam,
    MethodSyncNoParam,
    MethodSyncSingleParam,
    ProtocolParamType,
    ProtocolReturnType,
    ProtocolSelfType,
)

__all__ = [
    "as_value_handle",
]


@dataclass(frozen=True)
class _ValueHandleCall:
    """Marker wrapping the activity/workflow that :py:func:`as_value_handle` was given.

    Deliberately not callable: it is a declaration consumed by the start call,
    never something to invoke.
    """

    fn: Any


# The overloads mirror the start/execute call surface's own protocol families, so
# wrapping the callable rewrites only its *return* type. That is what lets every
# existing overload deliver a handle without gaining a handle-specific twin.
#
# Order matters: an unbound no-param method is structurally also a single-param
# callable, so the Method* protocols must be tried first, and async before sync at
# each arity (a coroutine function otherwise matches a sync protocol with
# ``ReturnType`` bound to the awaitable). Each async form is therefore a
# structural subset of the sync form that follows it, which is what the
# overlap suppressions below acknowledge -- the same trade the SDK's own
# async/sync overload pairs make.
@overload
def as_value_handle(  # type: ignore[overload-overlap,reportOverlappingOverload]
    fn: MethodAsyncNoParam[ProtocolSelfType, ProtocolReturnType],
) -> MethodAsyncNoParam[
    ProtocolSelfType, temporalio.common.ValueHandle[ProtocolReturnType]
]: ...


@overload
def as_value_handle(
    fn: MethodSyncNoParam[ProtocolSelfType, ProtocolReturnType],
) -> MethodSyncNoParam[
    ProtocolSelfType, temporalio.common.ValueHandle[ProtocolReturnType]
]: ...


@overload
def as_value_handle(  # type: ignore[overload-overlap,reportOverlappingOverload]
    fn: MethodAsyncSingleParam[ProtocolSelfType, ProtocolParamType, ProtocolReturnType],
) -> MethodAsyncSingleParam[
    ProtocolSelfType,
    ProtocolParamType,
    temporalio.common.ValueHandle[ProtocolReturnType],
]: ...


@overload
def as_value_handle(
    fn: MethodSyncSingleParam[ProtocolSelfType, ProtocolParamType, ProtocolReturnType],
) -> MethodSyncSingleParam[
    ProtocolSelfType,
    ProtocolParamType,
    temporalio.common.ValueHandle[ProtocolReturnType],
]: ...


@overload
def as_value_handle(  # type: ignore[overload-overlap,reportOverlappingOverload]
    fn: CallableAsyncNoParam[ProtocolReturnType],
) -> CallableAsyncNoParam[temporalio.common.ValueHandle[ProtocolReturnType]]: ...


@overload
def as_value_handle(
    fn: CallableSyncNoParam[ProtocolReturnType],
) -> CallableSyncNoParam[temporalio.common.ValueHandle[ProtocolReturnType]]: ...


@overload
def as_value_handle(  # type: ignore[overload-overlap,reportOverlappingOverload]
    fn: CallableAsyncSingleParam[ProtocolParamType, ProtocolReturnType],
) -> CallableAsyncSingleParam[
    ProtocolParamType, temporalio.common.ValueHandle[ProtocolReturnType]
]: ...


@overload
def as_value_handle(
    fn: CallableSyncSingleParam[ProtocolParamType, ProtocolReturnType],
) -> CallableSyncSingleParam[
    ProtocolParamType, temporalio.common.ValueHandle[ProtocolReturnType]
]: ...


@overload
def as_value_handle(  # type: ignore[overload-overlap,overload-cannot-match,reportOverlappingOverload]
    fn: Callable[..., Awaitable[ProtocolReturnType]],
) -> Callable[..., Awaitable[temporalio.common.ValueHandle[ProtocolReturnType]]]: ...


@overload
def as_value_handle(  # type: ignore[overload-cannot-match]
    fn: Callable[..., ProtocolReturnType],
) -> Callable[..., temporalio.common.ValueHandle[ProtocolReturnType]]: ...


def as_value_handle(fn: Any) -> Any:
    """Declare that a call's result is consumed as a :py:class:`temporalio.common.ValueHandle`.

    Wrap the activity or child workflow at the *start/execute call site*::

        handle = await workflow.execute_activity(
            workflow.as_value_handle(my_activity),
            start_to_close_timeout=timedelta(seconds=30),
        )

    The callee is unchanged and still returns its declared type; the awaited
    result is a handle over that type. If the result was offloaded to external
    storage it is never downloaded into the workflow -- forward the handle to an
    activity (or acquire its value there) to avoid paying for data the workflow
    only routes.

    Declaring at the call site is what makes the deferral a guarantee: the intent
    is fixed while the input is being built, before the command exists, so it
    cannot be requested after the result has already been downloaded and decoded.
    It also survives replay, since it is re-declared by the workflow code itself.

    For a dynamic (string-named) activity or workflow there is no callable to
    wrap; pass ``result_type=ValueHandle[T]`` to the start/execute call instead.

    .. warning::
        This API is experimental.
    """
    if isinstance(fn, _ValueHandleCall):
        return fn
    if isinstance(fn, str) or not callable(fn):
        raise TypeError(
            "as_value_handle expects an activity or workflow callable. For a "
            "dynamic (string-named) call, pass result_type=ValueHandle[T] instead."
        )
    return _ValueHandleCall(fn)


def _unwrap_value_handle_call(  # pyright: ignore[reportUnusedFunction]
    target: Any,
) -> tuple[Any, bool]:
    """Split a start call's target into its callable and its handle declaration.

    Called while the start input is being built, so the declaration is fixed
    before the command exists and before any result can arrive. That ordering is
    the guarantee: deferral can never be requested for a result the worker has
    already retrieved and decoded.
    """
    if isinstance(target, _ValueHandleCall):
        return target.fn, True
    return target, False
