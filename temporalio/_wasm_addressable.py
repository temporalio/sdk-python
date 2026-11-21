"""Guest-side loader for build-generated Python subroutine stubs."""

import inspect
import sys
from collections.abc import Callable
from functools import wraps
from types import ModuleType
from typing import ParamSpec, TypeVar, cast

from temporalio.workflow._context import _Runtime

_P = ParamSpec("_P")
_R = TypeVar("_R")
_revisions: dict[tuple[str, bytes], ModuleType] = {}
_bodies: dict[tuple[str, bytes], Callable[..., object]] = {}


def _resolve(slot: str, module: str, package: str) -> Callable[..., object]:
    from wit_world import resolve_addressable  # type: ignore[import-not-found]

    digest, source = resolve_addressable(slot)
    key = (module, digest)
    body = _bodies.get(key)
    if body is None:
        module_source, separator, entry = source.rpartition(
            b"\n_temporal_addressable_entry = "
        )
        if not separator:
            raise ValueError(f"Missing addressable entry for {slot!r}")
        # Capsules for different entry points in one revision share module globals.
        revision_key = (module, module_source)
        revision = _revisions.get(revision_key)
        if revision is None:
            name = f"{module}.__temporal_addressable_{digest.hex()}"
            revision = ModuleType(name)
            revision.__package__ = package
            revision.__file__ = f"<temporal-addressable:{digest.hex()}>"
            sys.modules[name] = revision
            try:
                exec(
                    compile(
                        module_source, revision.__file__, "exec", dont_inherit=True
                    ),
                    revision.__dict__,
                )
            except BaseException:
                del sys.modules[name]
                raise
            _revisions[revision_key] = revision
        body = cast(Callable[..., object], vars(revision)[entry.strip().decode()])
        _bodies[key] = body
    return body


def bind(
    slot: str, module: str, package: str
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Dispatch normal workflow calls to the host-selected implementation."""

    def decorate(original: Callable[_P, _R]) -> Callable[_P, _R]:
        @wraps(original)
        def dispatch(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            runtime = _Runtime.maybe_current()
            # Discovery and read-only handlers can run without replay bindings.
            if runtime is None or runtime.workflow_is_read_only():
                return original(*args, **kwargs)
            implementation = cast(Callable[_P, _R], _resolve(slot, module, package))
            return implementation(*args, **kwargs)

        mark_coroutine = getattr(inspect, "markcoroutinefunction", None)
        if mark_coroutine is not None and inspect.iscoroutinefunction(original):
            mark_coroutine(dispatch)
        return dispatch

    return decorate
