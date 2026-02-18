"""Utilities for per-workflow time skipping in tests."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, overload

import google.protobuf.field_mask_pb2

import temporalio.api.common.v1
import temporalio.api.workflow.v1
import temporalio.api.workflowservice.v1
import temporalio.client
import temporalio.converter
from temporalio.client import _start_workflow_time_skipping_config


class WorkflowTimeSkipper:
    """Testing utility for per-workflow time skipping.

    Creates a client that automatically enables time skipping on started
    workflows, and provides methods to advance virtual time and inspect
    upcoming time points.

    Example::

        ts = WorkflowTimeSkipper(env.client)
        handle = await ts.client.start_workflow(
            MyWorkflow.run, id="wf-1", task_queue="tq"
        )
        result = await ts.advance(handle)
        print(result.virtual_time_offset)
    """

    def __init__(
        self,
        client: temporalio.client.Client,
        *,
        auto_skip: bool | WorkflowTimeSkippingAutoSkipConfig = False,
    ) -> None:
        """Create a workflow time skipper.

        Args:
            client: The client to use. A cloned client with a time-skipping
                interceptor will be created.
            auto_skip: Controls automatic time skipping for workflows started
                via :py:attr:`client`. If ``False`` (default), time points must
                be advanced manually via :py:meth:`advance`. If ``True``,
                auto-skip is enabled with default settings. Can also be a
                :py:class:`WorkflowTimeSkippingAutoSkipConfig` for custom
                auto-skip settings.
        """
        self._start_workflow_config = _auto_skip_to_config(auto_skip)
        # Clone the client with an interceptor that sets the time-skipping
        # config contextvar on each start_workflow call
        config = client.config()
        config["interceptors"] = [
            *config["interceptors"],
            _TimeSkippingConfigInterceptor(self),
        ]
        self._client = temporalio.client.Client(**config)

    @property
    def start_workflow_config(self) -> WorkflowTimeSkippingConfig:
        """Config applied to future start_workflow calls."""
        return self._start_workflow_config

    @start_workflow_config.setter
    def start_workflow_config(self, value: WorkflowTimeSkippingConfig) -> None:
        self._start_workflow_config = value

    @property
    def client(self) -> temporalio.client.Client:
        """Client that applies time-skipping config on started workflows."""
        return self._client

    @overload
    async def advance(
        self,
        handle: temporalio.client.WorkflowHandle[Any, Any],
        /,
        *,
        up_to: datetime | timedelta | None = None,
        auto_skip: bool | WorkflowTimeSkippingAutoSkipConfig | None = None,
    ) -> WorkflowTimeSkippingAdvanceResult: ...

    @overload
    async def advance(
        self,
        id: str,
        /,
        *,
        run_id: str | None = None,
        up_to: datetime | timedelta | None = None,
        auto_skip: bool | WorkflowTimeSkippingAutoSkipConfig | None = None,
    ) -> WorkflowTimeSkippingAdvanceResult: ...

    async def advance(
        self,
        handle_or_id: temporalio.client.WorkflowHandle[Any, Any] | str,
        /,
        *,
        run_id: str | None = None,
        up_to: datetime | timedelta | None = None,
        auto_skip: bool | WorkflowTimeSkippingAutoSkipConfig | None = None,
    ) -> WorkflowTimeSkippingAdvanceResult:
        """Advance to the next time point for the given workflow.

        Fires all pending timers or timeouts at the earliest upcoming time,
        advancing virtual time. Optionally updates the auto-skip config in
        the same RPC call.

        Args:
            handle_or_id: A workflow handle or a workflow ID string.
            run_id: Run ID, only used when a workflow ID string is provided.
            up_to: Optional upper bound for the advance. Virtual time will
                advance to the earlier of the next time point or this value.
                If no time point exists before this value, virtual time
                advances to this value without firing any time points.
                An absolute ``datetime`` or a relative ``timedelta`` from the
                workflow's current virtual time.
            auto_skip: If not ``None``, updates the auto-skip config atomically
                with the advance. ``True`` enables auto-skip with defaults,
                ``False`` disables auto-skip, or pass a
                :py:class:`WorkflowTimeSkippingAutoSkipConfig` for custom
                settings.

        Returns:
            Result with the post-advance time-skipping state and the time
            points that were fired.
        """
        workflow_id, workflow_run_id = _resolve_workflow_ids(handle_or_id, run_id)
        req = temporalio.api.workflowservice.v1.AdvanceWorkflowExecutionTimePointRequest(
            namespace=self._client.namespace,
            workflow_execution=temporalio.api.common.v1.WorkflowExecution(
                workflow_id=workflow_id,
                run_id=workflow_run_id,
            ),
            identity=self._client.identity,
            request_id=str(uuid.uuid4()),
            time_skipping_config=_auto_skip_to_config(auto_skip)._to_proto()
            if auto_skip is not None
            else None,
        )
        if isinstance(up_to, datetime):
            req.up_to_time.FromDatetime(up_to)
        elif isinstance(up_to, timedelta):
            req.up_to_duration.FromTimedelta(up_to)
        resp = await self._client.workflow_service.advance_workflow_execution_time_point(
            req,
            retry=True,
        )
        return WorkflowTimeSkippingAdvanceResult(
            time_skipping=(
                WorkflowTimeSkippingInfo._from_proto(resp.time_skipping_info)
                if resp.HasField("time_skipping_info")
                else WorkflowTimeSkippingInfo(
                    config=None, virtual_time_offset=None, virtual_time=None,
                    raw=resp.time_skipping_info,
                )
            ),
            advanced_time_points=WorkflowTimeSkippingTimePoint._from_protos(
                resp.advanced_time_points
            ),
            upcoming_time_points=WorkflowTimeSkippingTimePoint._from_protos(
                resp.upcoming_time_points
            ),
        )

    @overload
    async def describe(
        self,
        handle: temporalio.client.WorkflowHandle[Any, Any],
        /,
    ) -> WorkflowTimeSkippingDescription: ...

    @overload
    async def describe(
        self,
        id: str,
        /,
        *,
        run_id: str | None = None,
    ) -> WorkflowTimeSkippingDescription: ...

    async def describe(
        self,
        handle_or_id: temporalio.client.WorkflowHandle[Any, Any] | str,
        /,
        *,
        run_id: str | None = None,
    ) -> WorkflowTimeSkippingDescription:
        """Get the current time-skipping state for the given workflow.

        Args:
            handle_or_id: A workflow handle or a workflow ID string.
            run_id: Run ID, only used when a workflow ID string is provided.

        Returns:
            Description of the time-skipping state.
        """
        workflow_id, workflow_run_id = _resolve_workflow_ids(handle_or_id, run_id)
        resp = await self._client.workflow_service.describe_workflow_execution(
            temporalio.api.workflowservice.v1.DescribeWorkflowExecutionRequest(
                namespace=self._client.namespace,
                execution=temporalio.api.common.v1.WorkflowExecution(
                    workflow_id=workflow_id,
                    run_id=workflow_run_id,
                ),
            ),
            retry=True,
        )
        return await WorkflowTimeSkippingDescription._from_raw_description(
            resp,
            namespace=self._client.namespace,
            converter=self._client.data_converter,
        )

    @overload
    async def update_skipping(
        self,
        handle: temporalio.client.WorkflowHandle[Any, Any],
        /,
        *,
        auto_skip: bool | WorkflowTimeSkippingAutoSkipConfig,
    ) -> None: ...

    @overload
    async def update_skipping(
        self,
        id: str,
        /,
        *,
        auto_skip: bool | WorkflowTimeSkippingAutoSkipConfig,
        run_id: str | None = None,
    ) -> None: ...

    async def update_skipping(
        self,
        handle_or_id: temporalio.client.WorkflowHandle[Any, Any] | str,
        /,
        *,
        auto_skip: bool | WorkflowTimeSkippingAutoSkipConfig,
        run_id: str | None = None,
    ) -> None:
        """Update auto-skip config on an already-running workflow.

        Uses UpdateWorkflowExecutionOptions under the hood.

        Args:
            handle_or_id: A workflow handle or a workflow ID string.
            run_id: Run ID, only used when a workflow ID string is provided.
            auto_skip: ``True`` enables auto-skip with defaults, ``False``
                disables auto-skip, or pass a
                :py:class:`WorkflowTimeSkippingAutoSkipConfig` for custom
                settings.
        """
        workflow_id, workflow_run_id = _resolve_workflow_ids(handle_or_id, run_id)
        ts_proto = _auto_skip_to_config(auto_skip)._to_proto()
        await self._client.workflow_service.update_workflow_execution_options(
            temporalio.api.workflowservice.v1.UpdateWorkflowExecutionOptionsRequest(
                namespace=self._client.namespace,
                workflow_execution=temporalio.api.common.v1.WorkflowExecution(
                    workflow_id=workflow_id,
                    run_id=workflow_run_id,
                ),
                workflow_execution_options=temporalio.api.workflow.v1.WorkflowExecutionOptions(
                    time_skipping_config=ts_proto,
                ),
                update_mask=google.protobuf.field_mask_pb2.FieldMask(
                    paths=["time_skipping_config"],
                ),
                identity=self._client.identity,
            ),
            retry=True,
        )


@dataclass(frozen=True)
class WorkflowTimeSkippingAutoSkipConfig:
    """Configuration for automatic time skipping.

    Auto-skip advances the workflow's virtual time to the next upcoming time
    point whenever the workflow is idle (no in-flight activities or Nexus
    operations). Auto-skip halts when either the time bound or the firings
    limit is reached.
    """

    until: datetime | timedelta | None = None
    """Bound for auto-skipping. An absolute ``datetime`` or a relative
    ``timedelta`` from the workflow's current virtual time."""

    max_firings: int = 0
    """Maximum number of time-point firings auto-skip will trigger before
    halting. Defaults to 10 on the server if not set or set to 0."""

    @staticmethod
    def _from_proto(
        proto: temporalio.api.workflow.v1.TimeSkippingConfig.AutoSkipConfig,
    ) -> WorkflowTimeSkippingAutoSkipConfig:
        if proto.HasField("until_time"):
            until: datetime | timedelta | None = (
                proto.until_time.ToDatetime().replace(tzinfo=timezone.utc)
            )
        elif proto.HasField("until_duration"):
            until = proto.until_duration.ToTimedelta()
        else:
            until = None
        return WorkflowTimeSkippingAutoSkipConfig(
            until=until,
            max_firings=proto.max_firings,
        )


@dataclass(frozen=True)
class WorkflowTimeSkippingConfig:
    """Configuration for per-workflow time skipping.

    This config can be applied to workflows at start time via
    :py:attr:`WorkflowTimeSkipper.client`, or to already-running workflows via
    :py:meth:`WorkflowTimeSkipper.update_skipping`. Once enabled on a workflow,
    time skipping cannot be disabled.
    """

    enabled: bool
    """Enables time skipping for this workflow execution."""

    auto_skip: WorkflowTimeSkippingAutoSkipConfig | None
    """If set, enables automatic time skipping. Set to ``None`` to disable
    auto-skip and return to manual-only time skipping."""

    propagate_to_new_children: bool = False
    """If true, newly started child workflows will automatically inherit this
    time-skipping configuration. The inherited config includes this flag, so
    grandchildren will also inherit (transitive)."""

    propagate_on_continue_as_new: bool = True
    """If true, continue-as-new will inherit this time-skipping configuration
    into the new run. Defaults to true since losing time-skipping on
    continue-as-new is rarely desired."""

    def _to_proto(self) -> temporalio.api.workflow.v1.TimeSkippingConfig:
        proto = temporalio.api.workflow.v1.TimeSkippingConfig(
            enabled=self.enabled,
            propagate_to_new_children=self.propagate_to_new_children,
            propagate_on_continue_as_new=self.propagate_on_continue_as_new,
        )
        if self.auto_skip is not None:
            proto.auto_skip.max_firings = self.auto_skip.max_firings
            if isinstance(self.auto_skip.until, datetime):
                proto.auto_skip.until_time.FromDatetime(self.auto_skip.until)
            elif isinstance(self.auto_skip.until, timedelta):
                proto.auto_skip.until_duration.FromTimedelta(self.auto_skip.until)
        return proto

    @staticmethod
    def _from_proto(
        proto: temporalio.api.workflow.v1.TimeSkippingConfig,
    ) -> WorkflowTimeSkippingConfig:
        return WorkflowTimeSkippingConfig(
            enabled=proto.enabled,
            auto_skip=(
                WorkflowTimeSkippingAutoSkipConfig._from_proto(proto.auto_skip)
                if proto.HasField("auto_skip")
                else None
            ),
            propagate_to_new_children=proto.propagate_to_new_children,
            propagate_on_continue_as_new=proto.propagate_on_continue_as_new,
        )


@dataclass(frozen=True)
class WorkflowTimeSkippingTimePoint:
    """An upcoming time point for a workflow execution."""

    fire_time: datetime
    """The absolute virtual time at which this time point will fire."""

    fire_time_remaining: timedelta
    """The remaining virtual time until this time point fires."""

    raw: temporalio.api.workflow.v1.UpcomingTimePointInfo
    """The raw proto for detailed type-specific info (timer, activity timeout,
    workflow timeout, etc.)."""

    @staticmethod
    def _from_protos(
        protos: Sequence[temporalio.api.workflow.v1.UpcomingTimePointInfo],
    ) -> list[WorkflowTimeSkippingTimePoint]:
        return [
            WorkflowTimeSkippingTimePoint(
                fire_time=tp.fire_time.ToDatetime().replace(tzinfo=timezone.utc),
                fire_time_remaining=tp.fire_time_remaining.ToTimedelta(),
                raw=tp,
            )
            for tp in protos
        ]


@dataclass(frozen=True)
class WorkflowTimeSkippingInfo:
    """Runtime time-skipping state for a workflow execution."""

    config: WorkflowTimeSkippingConfig | None
    """Time-skipping config on this workflow, or ``None`` if not enabled."""

    virtual_time_offset: timedelta | None
    """The accumulated virtual time offset, or ``None`` if time skipping is
    not enabled."""

    virtual_time: datetime | None
    """The workflow's current virtual time (server wall clock + offset)."""

    raw: temporalio.api.workflow.v1.TimeSkippingInfo
    """The raw time-skipping info."""

    @staticmethod
    def _from_proto(
        proto: temporalio.api.workflow.v1.TimeSkippingInfo,
    ) -> WorkflowTimeSkippingInfo:
        return WorkflowTimeSkippingInfo(
            config=(
                WorkflowTimeSkippingConfig._from_proto(proto.config)
                if proto.HasField("config")
                else None
            ),
            virtual_time_offset=proto.virtual_time_offset.ToTimedelta(),
            virtual_time=(
                proto.virtual_time.ToDatetime().replace(tzinfo=timezone.utc)
                if proto.HasField("virtual_time")
                else None
            ),
            raw=proto,
        )


@dataclass(frozen=True)
class WorkflowTimeSkippingAdvanceResult:
    """Result of advancing a workflow's virtual time to the next time point."""

    time_skipping: WorkflowTimeSkippingInfo
    """Post-advance time-skipping state (config, offset)."""

    advanced_time_points: Sequence[WorkflowTimeSkippingTimePoint]
    """The time points that were fired. Empty when there was no upcoming time
    point to advance to."""

    upcoming_time_points: Sequence[WorkflowTimeSkippingTimePoint]
    """Remaining upcoming time points after the advance."""


@dataclass
class WorkflowTimeSkippingDescription(temporalio.client.WorkflowExecutionDescription):
    """Workflow execution description with time-skipping state."""

    time_skipping: WorkflowTimeSkippingInfo | None = None
    """Time-skipping state, or ``None`` if time skipping is not enabled."""

    upcoming_time_points: Sequence[WorkflowTimeSkippingTimePoint] = ()
    """Pending time points for this workflow."""

    @classmethod
    async def _from_raw_description(
        cls,
        description: temporalio.api.workflowservice.v1.DescribeWorkflowExecutionResponse,
        namespace: str,
        converter: temporalio.converter.DataConverter,
        **additional_fields: Any,
    ) -> WorkflowTimeSkippingDescription:
        has_ts = description.HasField("time_skipping_info")
        return await super()._from_raw_description(
            description,
            namespace=namespace,
            converter=converter,
            time_skipping=(
                WorkflowTimeSkippingInfo._from_proto(description.time_skipping_info)
                if has_ts
                else None
            ),
            upcoming_time_points=WorkflowTimeSkippingTimePoint._from_protos(
                description.upcoming_time_points
            ),
            **additional_fields,
        )


def _auto_skip_to_config(
    auto_skip: bool | WorkflowTimeSkippingAutoSkipConfig,
) -> WorkflowTimeSkippingConfig:
    if isinstance(auto_skip, WorkflowTimeSkippingAutoSkipConfig):
        auto_skip_config: WorkflowTimeSkippingAutoSkipConfig | None = auto_skip
    elif auto_skip:
        auto_skip_config = WorkflowTimeSkippingAutoSkipConfig()
    else:
        auto_skip_config = None
    return WorkflowTimeSkippingConfig(enabled=True, auto_skip=auto_skip_config)


def _resolve_workflow_ids(
    handle_or_id: temporalio.client.WorkflowHandle[Any, Any] | str,
    run_id: str | None,
) -> tuple[str, str]:
    if isinstance(handle_or_id, str):
        return handle_or_id, run_id or ""
    return handle_or_id.id, handle_or_id.run_id or ""


class _TimeSkippingConfigInterceptor(temporalio.client.Interceptor):
    def __init__(self, skipper: WorkflowTimeSkipper) -> None:  # type: ignore[reportMissingSuperCall]
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
        proto = self._skipper.start_workflow_config._to_proto()
        token = _start_workflow_time_skipping_config.set(proto)
        try:
            return await super().start_workflow(input)
        finally:
            _start_workflow_time_skipping_config.reset(token)
