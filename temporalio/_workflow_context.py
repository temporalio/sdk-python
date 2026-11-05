"""Private workflow compatibility flags and their runtime context."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from temporalio.worker._workflow_instance import _WorkflowInstanceImpl


class _WorkflowLogicFlag(IntEnum):
    """Flags recorded in workflow task metadata to preserve replay behavior."""

    RAISE_ON_CANCELLING_COMPLETED_ACTIVITY = 1
    PROCESS_WORKFLOW_ACTIVATION_JOBS_AS_SINGLE_BATCH = 2
    DECODE_DATETIME_PROXIES = 3


_DEFAULT_ENABLED_WORKFLOW_LOGIC_FLAGS: frozenset[_WorkflowLogicFlag] = frozenset(
    {
        _WorkflowLogicFlag.PROCESS_WORKFLOW_ACTIVATION_JOBS_AS_SINGLE_BATCH,
        _WorkflowLogicFlag.DECODE_DATETIME_PROXIES,
    }
)


@dataclass(frozen=True)
class _WorkflowFlagContext:
    _runtime: _WorkflowInstanceImpl

    def is_flag_enabled(self, flag: _WorkflowLogicFlag) -> bool:
        """Consult live runtime state, not the state at context creation."""
        return self._runtime._workflow_logic_flag_enabled(flag)


_current_workflow_flags: ContextVar[_WorkflowFlagContext | None] = ContextVar(
    "temporal_workflow_flags", default=None
)


@contextmanager
def _workflow_flag_scope(  # type:ignore[reportUnusedFunction]
    *,
    runtime: _WorkflowInstanceImpl,
) -> Generator[None]:
    """Expose live flag decisions without inheriting another workflow's state."""
    context = _WorkflowFlagContext(
        _runtime=runtime,
    )
    token = _current_workflow_flags.set(context)
    try:
        yield
    finally:
        _current_workflow_flags.reset(token)
