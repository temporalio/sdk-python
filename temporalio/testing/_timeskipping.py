"""Utilities for per-workflow time skipping in tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import google.protobuf.field_mask_pb2

import temporalio.api.common.v1
import temporalio.api.enums.v1.event_type_pb2 as _event_type
import temporalio.api.workflow.v1
import temporalio.api.workflowservice.v1
import temporalio.client
from temporalio.client._impl import _start_workflow_time_skipping_config


@dataclass(frozen=True)
class WorkflowTimeSkippingConfig:
    """Per-workflow time skipping configuration."""

    enabled: bool = True
    """Whether time skipping is enabled for the workflow."""

    max_skip_duration: timedelta | None = None
    """Maximum total virtual time that can be skipped before time skipping
    is automatically disabled."""

    def _to_proto(self) -> temporalio.api.workflow.v1.TimeSkippingConfig:
        proto = temporalio.api.workflow.v1.TimeSkippingConfig(enabled=self.enabled)
        if self.max_skip_duration is not None:
            proto.max_skipped_duration.FromTimedelta(self.max_skip_duration)
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


class WorkflowTimeSkipper:
    """Testing utility for per-workflow time skipping.

    Creates a cloned client that automatically enables time skipping on every
    workflow started through it. Once a workflow's configured bound is
    reached, :py:meth:`wait_for_skip_duration_reached` blocks until the
    transition occurs and :py:meth:`resume` re-enables skipping with an
    optional new delta.

    Example::

        ts = WorkflowTimeSkipper(env.client,
            config=WorkflowTimeSkippingConfig(max_skip_duration=timedelta(hours=1)))

        handle = await ts.client.start_workflow(
            MyWorkflow.run, id="wf-1", task_queue="tq",
        )
        await ts.wait_for_skip_duration_reached(handle)
        # inspect state, signal, etc.
        await ts.resume(handle, delta=timedelta(hours=1))
        result = await handle.result()

    Works against any client the test suite hands in (local, self-hosted, or
    cloud). TODO: cloud usage assumes the namespace has server-side time
    skipping enabled (``frontend.TimeSkippingEnabled``); add a ``cloud``
    fixture mode alongside ``local`` / ``time-skipping`` in ``conftest.env``
    so the same tests can be pointed at a cloud namespace once that lands.
    """

    def __init__(
        self,
        client: temporalio.client.Client,
        *,
        config: WorkflowTimeSkippingConfig = WorkflowTimeSkippingConfig(),
    ) -> None:
        """Create a workflow time skipper.

        Args:
            client: The client to wrap. A cloned client with a time-skipping
                interceptor is created; the original is left untouched.
            config: Initial bound. Defaults to no bound — time skipping runs
                until the workflow completes.
        """
        self._config = config
        client_config = client.config()
        client_config["interceptors"] = [
            *client_config["interceptors"],
            _TimeSkippingConfigInterceptor(self),
        ]
        self._client = temporalio.client.Client(**client_config)
        # Per-workflow max_skip_duration last set on the server, keyed by
        # (workflow_id, run_id).
        self._bound_cache: dict[tuple[str, str], timedelta] = {}

    @property
    def client(self) -> temporalio.client.Client:
        """Client that enables time skipping on every started workflow."""
        return self._client

    @property
    def config(self) -> WorkflowTimeSkippingConfig:
        """Bound applied to future start_workflow calls."""
        return self._config

    @config.setter
    def config(self, value: WorkflowTimeSkippingConfig) -> None:
        self._config = value

    async def wait_for_skip_duration_reached(
        self,
        handle: temporalio.client.WorkflowHandle[Any, Any],
    ) -> bool:
        """Block until the workflow's configured skip duration is reached.

        Returns ``True`` once a time-skipping-disabled transition is observed.
        Returns ``False`` if the workflow terminates before any bound is
        reached.
        """
        # TODO: Replace with a dedicated long-poll RPC once the server adds
        # one for time-skipping transitions. The current path streams every
        # history event since the workflow started, which is correct but not
        # the most efficient if event volume is high.
        async for event in handle.fetch_history_events(wait_new_event=True):
            if (
                event.event_type
                == _event_type.EVENT_TYPE_WORKFLOW_EXECUTION_TIME_SKIPPING_TRANSITIONED
            ):
                attrs = (
                    event.workflow_execution_time_skipping_transitioned_event_attributes
                )
                if attrs.disabled_after_bound:
                    return True
            elif event.event_type in _TERMINAL_EVENT_TYPES:
                return False
        return False

    async def resume(
        self,
        handle: temporalio.client.WorkflowHandle[Any, Any],
        delta: timedelta | None = None,
    ) -> None:
        """Re-enable time skipping after a bound was reached.

        With ``delta``, sets a new bound equal to (previously-set bound +
        delta). Without ``delta``, resumes skipping with no bound — the
        workflow auto-skips until completion.
        """
        proto = temporalio.api.workflow.v1.TimeSkippingConfig(enabled=True)
        if delta is not None:
            cache_key = (handle.id, handle.run_id or "")
            if cache_key not in self._bound_cache:
                if self._config.max_skip_duration is None:
                    raise ValueError(
                        "resume(delta=...) requires an initial bound to have been "
                        "configured on the WorkflowTimeSkipper, or call resume() "
                        "with no delta to resume unbounded."
                    )
                self._bound_cache[cache_key] = self._config.max_skip_duration
            new_value = self._bound_cache[cache_key] + delta
            proto.max_skipped_duration.FromTimedelta(new_value)
            self._bound_cache[cache_key] = new_value

        await self._client.workflow_service.update_workflow_execution_options(
            temporalio.api.workflowservice.v1.UpdateWorkflowExecutionOptionsRequest(
                namespace=self._client.namespace,
                workflow_execution=temporalio.api.common.v1.WorkflowExecution(
                    workflow_id=handle.id,
                    run_id=handle.run_id or "",
                ),
                workflow_execution_options=temporalio.api.workflow.v1.WorkflowExecutionOptions(
                    time_skipping_config=proto,
                ),
                update_mask=google.protobuf.field_mask_pb2.FieldMask(
                    paths=["time_skipping_config"],
                ),
                identity=self._client.identity,
            ),
            retry=True,
        )


class _TimeSkippingConfigInterceptor(temporalio.client.Interceptor):
    def __init__(self, skipper: WorkflowTimeSkipper) -> None:
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
        skipper: WorkflowTimeSkipper,
    ) -> None:
        super().__init__(next)
        self._skipper = skipper

    async def start_workflow(
        self, input: temporalio.client.StartWorkflowInput
    ) -> temporalio.client.WorkflowHandle[Any, Any]:
        proto = self._skipper.config._to_proto()
        token = _start_workflow_time_skipping_config.set(proto)
        try:
            handle = await super().start_workflow(input)
        finally:
            _start_workflow_time_skipping_config.reset(token)
        # Seed the bound cache so future resume(delta=...) calls have a
        # baseline to add to. Captures the config at start time, even if the
        # user mutates self._skipper.config afterwards.
        cfg = self._skipper.config
        if cfg.max_skip_duration is not None:
            cache_key = (handle.id, handle.run_id or "")
            self._skipper._bound_cache[cache_key] = cfg.max_skip_duration
        return handle
