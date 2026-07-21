"""Per-workflow time-skipping API and testing helper."""

from __future__ import annotations

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


@dataclass(frozen=True)
class TimeSkippingConfig:
    """Per-workflow time skipping configuration."""

    enabled: bool = True
    """Whether time skipping is enabled for the workflow."""

    fast_forward: timedelta | None = None
    """One-shot advance of virtual time by this duration. Time skipping auto-disables at
    the target time. ``timedelta=None`` means unbounded advance until completion."""

    disable_propagation: bool = False
    """If true, child workflows do not inherit the ``enabled`` flag. Virtual
    start time inherits regardless."""

    def _to_proto(self) -> temporalio.api.common.v1.TimeSkippingConfig:
        proto = temporalio.api.common.v1.TimeSkippingConfig(
            enabled=self.enabled,
            disable_child_propagation=self.disable_propagation,
        )
        if self.fast_forward is not None:
            proto.fast_forward.FromTimedelta(self.fast_forward)
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
    event to fire), and :py:meth:`set_disable_propagation` to toggle the
    propagation flag on a running workflow.

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
        ``TimeSkippingConfig``, then waits for the resulting
        ``disabled_after_fast_forward`` transition event (or the workflow's
        terminal event).

        Args:
            handle: Target workflow execution.
            duration: One-shot advance by this amount (``timedelta`` or
                seconds as a ``float``). If ``None``, resume unbounded
                skipping until completion.

        Returns:
            True if the fast-forward transition is observed; False if the
            workflow terminates first.
        """
        if duration is not None and not isinstance(duration, timedelta):
            duration = timedelta(seconds=duration)
        # Capture the most recent transition event's id before we issue this
        # FF. The wait loop will only consider transition events with a
        # higher event_id, ensuring we don't spuriously return True from a
        # transition left in history by a previous FF on the same workflow.
        latest_transition_event_id = await self._latest_transition_event_id(handle)
        await self._update_time_skipping_config(
            handle,
            TimeSkippingConfig(
                enabled=True,
                fast_forward=duration,
                disable_propagation=self._config.disable_propagation,
            ),
        )
        return await self._wait_for_fast_forward_completed(
            handle, latest_transition_event_id
        )

    async def set_disable_propagation(
        self,
        handle: temporalio.client.WorkflowHandle[Any, Any],
        disable_propagation: bool,
        /,
    ) -> None:
        """Set the ``disable_propagation`` flag on a running workflow.

        Args:
            handle: Target workflow execution.
            disable_propagation: New value for the flag.
        """
        await self._update_time_skipping_config(
            handle,
            TimeSkippingConfig(
                enabled=True,
                disable_propagation=disable_propagation,
            ),
        )

    async def _latest_transition_event_id(
        self,
        handle: temporalio.client.WorkflowHandle[Any, Any],
    ) -> int:
        """Return the event_id of the most recent time-skipping transition event.

        Returns 0 if the workflow has no such events yet.
        """
        latest = 0
        async for event in handle.fetch_history_events():
            if (
                event.event_type
                == _event_type.EVENT_TYPE_WORKFLOW_EXECUTION_TIME_SKIPPING_TRANSITIONED
            ):
                latest = event.event_id
        return latest

    async def _wait_for_fast_forward_completed(
        self,
        handle: temporalio.client.WorkflowHandle[Any, Any],
        last_transition_event_id: int = 0,
    ) -> bool:
        """Wait for a ``disabled_after_fast_forward`` transition event.

        Args:
            handle: Target workflow execution.
            last_transition_event_id: Ignore transition events with an
                ``event_id`` at or below this value — they belong to earlier
                FFs on this workflow and shouldn't be treated as this FF's
                completion.

        Returns:
            True on a fresh ``disabled_after_fast_forward`` transition;
            False if the workflow terminates first.
        """
        async for event in handle.fetch_history_events(wait_new_event=True):
            if (
                event.event_type
                == _event_type.EVENT_TYPE_WORKFLOW_EXECUTION_TIME_SKIPPING_TRANSITIONED
            ):
                if event.event_id <= last_transition_event_id:
                    continue
                attrs = (
                    event.workflow_execution_time_skipping_transitioned_event_attributes
                )
                if attrs.disabled_after_fast_forward:
                    return True
            elif event.event_type in _TERMINAL_EVENT_TYPES:
                return False
        return False

    async def _update_time_skipping_config(
        self,
        handle: temporalio.client.WorkflowHandle[Any, Any],
        config: TimeSkippingConfig,
    ) -> temporalio.api.workflowservice.v1.UpdateWorkflowExecutionOptionsResponse:
        """Replace the stored time-skipping config for a running workflow.

        The server accepts the whole ``TimeSkippingConfig`` field only, so
        callers must send a complete config. Public callers should prefer
        the per-field updaters (:py:meth:`fast_forward`,
        :py:meth:`set_disable_propagation`).
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
