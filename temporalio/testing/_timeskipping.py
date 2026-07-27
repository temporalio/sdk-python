"""Per-workflow time-skipping API and testing helper."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import google.protobuf.field_mask_pb2

import temporalio.api.common.v1
import temporalio.api.enums.v1.event_type_pb2 as _event_type
import temporalio.api.workflow.v1
import temporalio.api.workflowservice.v1
import temporalio.client
from temporalio.api.workflowservice.v1 import PollWorkflowExecutionTimeSkippingResponse


@dataclass(frozen=True)
class FastForwardConfig:
    """One-shot fast-forward within a :py:class:`TimeSkippingConfig`."""

    id: str
    """Identifies this fast-forward for ``PollWorkflowExecutionTimeSkipping``."""

    duration: timedelta
    """Advance the workflow's virtual time by this amount. Time skipping
    auto-disables when the target time is reached."""

    def _to_proto(self) -> temporalio.api.common.v1.FastForwardConfig:
        proto = temporalio.api.common.v1.FastForwardConfig(id=self.id)
        proto.duration.FromTimedelta(self.duration)
        return proto


@dataclass(frozen=True)
class TimeSkippingConfig:
    """Per-workflow time skipping configuration."""

    enabled: bool = True
    """Whether time skipping is enabled for the workflow."""

    fast_forward_config: FastForwardConfig | None = None
    """One-shot fast-forward. ``None`` means skip unbounded until completion
    (when :py:attr:`enabled` is true) or no skipping at all (when it is false)."""

    disable_propagation: bool = False
    """If true, child workflows do not inherit the ``enabled`` flag. Virtual
    start time inherits regardless."""

    def __post_init__(self) -> None:
        """Validates that a fast-forward isn't configured with skipping off."""
        if not self.enabled and self.fast_forward_config is not None:
            raise ValueError(
                "fast_forward_config cannot be set when enabled is False"
            )

    def _to_proto(self) -> temporalio.api.common.v1.TimeSkippingConfig:
        proto = temporalio.api.common.v1.TimeSkippingConfig(
            enabled=self.enabled,
            disable_propagation=self.disable_propagation,
        )
        if self.fast_forward_config is not None:
            proto.fast_forward_config.CopyFrom(self.fast_forward_config._to_proto())
        return proto


_TERMINAL_EVENT_TYPES = frozenset(
    {
        _event_type.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED,
        _event_type.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED,
        _event_type.EVENT_TYPE_WORKFLOW_EXECUTION_TIMED_OUT,
        _event_type.EVENT_TYPE_WORKFLOW_EXECUTION_TERMINATED,
        _event_type.EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED,
        _event_type.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW,
    }
)


class TimeSkipper:
    """Client wrapper for per-workflow time skipping.

    Wraps a client with an interceptor that stamps a ``TimeSkippingConfig``
    on every workflow started through :py:attr:`client`. Use
    :py:meth:`fast_forward` to advance a running workflow's virtual clock
    (awaited call waits for the fast-forward to complete and the transition
    event to fire).

    In tests, the same functionality is available via
    :py:class:`WorkflowEnvironment.start_time_skipping_v2`. Use
    ``TimeSkipper`` directly outside test environments.
    """

    def __init__(
        self,
        client: temporalio.client.Client,
        *,
        config: TimeSkippingConfig = TimeSkippingConfig(),
    ) -> None:
        """Create a time skipper.

        Args:
            client: Client to wrap. A cloned client is created; the original
                is untouched.
            config: Default config stamped on every workflow started via
                :py:attr:`client`.
        """
        self._config = config
        self._ts_enabled = True
        client_config = client.config()
        client_config["interceptors"] = [
            *client_config["interceptors"],
            _TimeSkippingConfigInterceptor(self),
        ]
        self._client = temporalio.client.Client(**client_config)

    @property
    def client(self) -> temporalio.client.Client:
        """Client that stamps time-skipping config on every started workflow."""
        return self._client

    @property
    def config(self) -> TimeSkippingConfig:
        """Configuration applied to future start_workflow calls."""
        return self._config

    @config.setter
    def config(self, value: TimeSkippingConfig) -> None:
        self._config = value

    async def fast_forward(
        self,
        handle: temporalio.client.WorkflowHandle[Any, Any],
        duration: timedelta | float | None = None,
        /,
    ) -> bool:
        """Issue a fast-forward on a workflow and wait for it to complete.

        Sends an ``UpdateWorkflowExecutionOptions`` with the new
        ``TimeSkippingConfig``. For a bounded ``duration``, long-polls
        ``PollWorkflowExecutionTimeSkipping`` for completion, which
        follows the workflow across multiple runs.

        Args:
            handle: Target workflow execution.
            duration: One-shot advance by this amount (``timedelta`` or
                seconds as a ``float``). If ``None``, enable unbounded
                skipping and wait for the workflow to terminate.

        Returns:
            For a bounded ``duration``: True if the fast-forward completes,
            False if the workflow chain terminates first or the fast-forward
            id is overridden. For ``duration=None``: always False (there is
            no fast-forward completion to observe; the wait ends on the
            workflow's terminal event).
        """
        if duration is not None and not isinstance(duration, timedelta):
            duration = timedelta(seconds=duration)
        if duration is None:
            return await self._wait_for_unbounded_fast_forward_completion(handle)
        fast_forward_id = str(uuid.uuid4())
        await self._update_time_skipping_config(
            handle,
            TimeSkippingConfig(
                enabled=True,
                fast_forward_config=FastForwardConfig(
                    id=fast_forward_id, duration=duration
                ),
                disable_propagation=self._config.disable_propagation,
            ),
        )
        return await self._poll_fast_forward_completion(handle, fast_forward_id)

    async def _wait_for_unbounded_fast_forward_completion(
        self,
        handle: temporalio.client.WorkflowHandle[Any, Any],
    ) -> bool:
        """Enable unbounded time skipping and wait for the workflow to terminate.

        There is no fast-forward id to poll on, so we watch history events
        for a terminal event on the current run. Always returns False —
        unbounded skipping has no completion event of its own; the wait
        ends on the workflow's terminal event.
        """
        await self._update_time_skipping_config(
            handle,
            TimeSkippingConfig(
                enabled=True,
                disable_propagation=self._config.disable_propagation,
            ),
        )
        async for event in handle.fetch_history_events(wait_new_event=True):
            if event.event_type in _TERMINAL_EVENT_TYPES:
                return False
        return False

    async def _poll_fast_forward_completion(
        self,
        handle: temporalio.client.WorkflowHandle[Any, Any],
        fast_forward_id: str,
    ) -> bool:
        """Long-poll for a bounded fast-forward to complete.

        Passes ``run_id=""`` so the server resolves to the current run in
        the chain (retry / CAN / cron) and follows the FF across the chain
        boundary. Retries on server-side long-poll timeout until a
        terminal poll result arrives.
        """
        req = (
            temporalio.api.workflowservice.v1.PollWorkflowExecutionTimeSkippingRequest(
                namespace=self._client.namespace,
                workflow_execution=temporalio.api.common.v1.WorkflowExecution(
                    workflow_id=handle.id,
                    run_id="",
                ),
                fast_forward_id=fast_forward_id,
            )
        )
        while True:
            resp = await self._client.workflow_service.poll_workflow_execution_time_skipping(
                req, retry=True
            )
            if resp.result == PollWorkflowExecutionTimeSkippingResponse.RESULT_FAST_FORWARD_COMPLETED:
                return True
            if (
                resp.result
                == PollWorkflowExecutionTimeSkippingResponse.RESULT_WORKFLOW_ENDED_BEFORE_FAST_FORWARD_COMPLETION
            ):
                return False
            if (
                resp.result
                == PollWorkflowExecutionTimeSkippingResponse.RESULT_FAST_FORWARD_ID_MISMATCH
            ):
                raise RuntimeError(
                    f"PollWorkflowExecutionTimeSkipping returned "
                    f"RESULT_FAST_FORWARD_ID_MISMATCH for id {fast_forward_id!r}: "
                    "the workflow's active fast-forward id no longer matches. "
                    "This is the expected result when another fast_forward() call "
                    "overrode this one; if the caller did not do that, it's an internal bug."
                )
            # RESULT_POLL_TIMEOUT (server-side long-poll expiry): re-poll.

    async def _update_time_skipping_config(
        self,
        handle: temporalio.client.WorkflowHandle[Any, Any],
        config: TimeSkippingConfig,
    ) -> temporalio.api.workflowservice.v1.UpdateWorkflowExecutionOptionsResponse:
        """Replace the stored time-skipping config for a running workflow.

        The server accepts the whole ``TimeSkippingConfig`` field only, so
        callers must send a complete config. Public callers should use
        :py:meth:`fast_forward`.
        """
        return await self._client.workflow_service.update_workflow_execution_options(
            temporalio.api.workflowservice.v1.UpdateWorkflowExecutionOptionsRequest(
                namespace=self._client.namespace,
                workflow_execution=temporalio.api.common.v1.WorkflowExecution(
                    workflow_id=handle.id,
                    run_id=handle.run_id or "",
                ),
                workflow_execution_options=temporalio.api.workflow.v1.WorkflowExecutionOptions(
                    time_skipping_config=config._to_proto(),
                ),
                update_mask=google.protobuf.field_mask_pb2.FieldMask(
                    paths=["time_skipping_config"],
                ),
                identity=self._client.identity,
            ),
            retry=True,
        )

    @contextmanager
    def with_time_skipping_disabled(self) -> Iterator[None]:
        """Suspend time-skipping config stamping on newly-started workflows within the block.

        Workflows started via :py:attr:`client` inside the block do not get
        their ``time_skipping_config`` set; existing workflows are unaffected.
        """
        was_enabled = self._ts_enabled
        self._ts_enabled = False
        try:
            yield None
        finally:
            self._ts_enabled = was_enabled


class _TimeSkippingConfigInterceptor(temporalio.client.Interceptor):
    def __init__(self, skipper: TimeSkipper) -> None:
        super().__init__()
        self._skipper = skipper

    def intercept_client(
        self, next: temporalio.client.OutboundInterceptor
    ) -> temporalio.client.OutboundInterceptor:
        return _TimeSkippingConfigOutbound(next, self._skipper)


class _TimeSkippingConfigOutbound(temporalio.client.OutboundInterceptor):
    def __init__(
        self,
        next: temporalio.client.OutboundInterceptor,
        skipper: TimeSkipper,
    ) -> None:
        super().__init__(next)
        self._skipper = skipper

    async def start_workflow(
        self, input: temporalio.client.StartWorkflowInput
    ) -> temporalio.client.WorkflowHandle[Any, Any]:
        if self._skipper._ts_enabled:
            input.time_skipping_config = self._skipper.config._to_proto()
        return await super().start_workflow(input)
