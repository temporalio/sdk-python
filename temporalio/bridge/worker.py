"""Worker using SDK Core. (unstable)

Nothing in this module should be considered stable. The API may change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Awaitable,
    Callable,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

import google.protobuf.internal.containers
from typing_extensions import TypeAlias

import temporalio.api.common.v1
import temporalio.api.history.v1
import temporalio.bridge.client
import temporalio.bridge.proto
import temporalio.bridge.proto.activity_task
import temporalio.bridge.proto.workflow_activation
import temporalio.bridge.proto.workflow_completion
import temporalio.bridge.runtime
import temporalio.bridge.temporal_sdk_bridge
import temporalio.converter
import temporalio.exceptions
from temporalio.bridge.temporal_sdk_bridge import PollShutdownError


@dataclass
class WorkerConfig:
    """Python representation of the Rust struct for configuring a worker."""

    namespace: str
    task_queue: str
    build_id: str
    identity_override: Optional[str]
    max_cached_workflows: int
    tuner: TunerHolder
    max_concurrent_workflow_task_polls: int
    nonsticky_to_sticky_poll_ratio: float
    max_concurrent_activity_task_polls: int
    no_remote_activities: bool
    sticky_queue_schedule_to_start_timeout_millis: int
    max_heartbeat_throttle_interval_millis: int
    default_heartbeat_throttle_interval_millis: int
    max_activities_per_second: Optional[float]
    max_task_queue_activities_per_second: Optional[float]
    graceful_shutdown_period_millis: int
    use_worker_versioning: bool
    nondeterminism_as_workflow_fail: bool
    nondeterminism_as_workflow_fail_for_types: Set[str]


@dataclass
class ResourceBasedTunerConfig:
    """Python representation of the Rust struct for configuring a resource-based tuner."""

    target_memory_usage: float
    target_cpu_usage: float


@dataclass
class ResourceBasedSlotSupplier:
    """Python representation of the Rust struct for a resource-based slot supplier."""

    minimum_slots: int
    maximum_slots: int
    ramp_throttle_ms: int
    tuner_config: ResourceBasedTunerConfig


@dataclass(frozen=True)
class FixedSizeSlotSupplier:
    """Python representation of the Rust struct for a fixed-size slot supplier."""

    num_slots: int


@dataclass(frozen=True)
class SlotPermit:
    """A permit to use a slot for a workflow/activity/local activity task."""

    pass


@dataclass(frozen=True)
class SlotReserveContext:
    """Context for reserving a slot from a :py:class:`CustomSlotSupplier`."""

    slot_type: str  # TODO real type
    """The type of slot trying to be reserved."""
    task_queue: str
    """The name of the task queue for which this reservation request is associated."""
    worker_identity: str
    """The identity of the worker that is requesting the reservation."""
    worker_build_id: str
    """The build id of the worker that is requesting the reservation."""
    is_sticky: bool
    """True iff this is a reservation for a sticky poll for a workflow task."""


@dataclass(frozen=True)
class SlotMarkUsedContext:
    """Context for marking a slot used from a :py:class:`CustomSlotSupplier`."""

    pass


@dataclass(frozen=True)
class SlotReleaseContext:
    """Context for releasing a slot from a :py:class:`CustomSlotSupplier`."""

    pass


class CustomSlotSupplier(ABC):
    """This class can be implemented to provide custom slot supplier behavior."""

    # TODO: AbortError equivalent
    @abstractmethod
    async def reserve_slot(self, ctx: SlotReserveContext) -> SlotPermit:
        """This function is called before polling for new tasks. Your implementation must block until a
        slot is available then return a permit to use that slot.

        The only acceptable exception to throw is AbortError, any other exceptions thrown will be
        logged and ignored.

        Args:
            ctx: The context for slot reservation.

        Returns:
            A permit to use the slot which may be populated with your own data.
        """
        ...

    @abstractmethod
    def try_reserve_slot(self, ctx: SlotReserveContext) -> Optional[SlotPermit]:
        """This function is called when trying to reserve slots for "eager" workflow and activity tasks.
        Eager tasks are those which are returned as a result of completing a workflow task, rather than
        from polling. Your implementation must not block, and if a slot is available, return a permit
        to use that slot.

        Args:
            ctx: The context for slot reservation.

        Returns:
            Maybe a permit to use the slot which may be populated with your own data.
        """
        ...

    @abstractmethod
    def mark_slot_used(self, ctx: SlotMarkUsedContext) -> None:
        """This function is called once a slot is actually being used to process some task, which may be
        some time after the slot was reserved originally. For example, if there is no work for a
        worker, a number of slots equal to the number of active pollers may already be reserved, but
        none of them are being used yet. This call should be non-blocking.

        Args:
            ctx: The context for marking a slot as used.
        """
        ...

    @abstractmethod
    def release_slot(self, ctx: SlotReleaseContext) -> None:
        """This function is called once a permit is no longer needed. This could be because the task has
        finished, whether successfully or not, or because the slot was no longer needed (ex: the number
        of active pollers decreased). This call should be non-blocking.

        Args:
            ctx: The context for releasing a slot.
        """
        ...


SlotSupplier: TypeAlias = Union[
    FixedSizeSlotSupplier, ResourceBasedSlotSupplier, CustomSlotSupplier
]


@dataclass
class TunerHolder:
    """Python representation of the Rust struct for a tuner holder."""

    workflow_slot_supplier: SlotSupplier
    activity_slot_supplier: SlotSupplier
    local_activity_slot_supplier: SlotSupplier


class Worker:
    """SDK Core worker."""

    @staticmethod
    def create(client: temporalio.bridge.client.Client, config: WorkerConfig) -> Worker:
        """Create a bridge worker from a bridge client."""
        return Worker(
            temporalio.bridge.temporal_sdk_bridge.new_worker(
                client._runtime._ref, client._ref, config
            )
        )

    @staticmethod
    def for_replay(
        runtime: temporalio.bridge.runtime.Runtime,
        config: WorkerConfig,
    ) -> Tuple[Worker, temporalio.bridge.temporal_sdk_bridge.HistoryPusher]:
        """Create a bridge replay worker."""
        [
            replay_worker,
            pusher,
        ] = temporalio.bridge.temporal_sdk_bridge.new_replay_worker(
            runtime._ref, config
        )
        return Worker(replay_worker), pusher

    def __init__(self, ref: temporalio.bridge.temporal_sdk_bridge.WorkerRef) -> None:
        """Create SDK core worker from a bridge worker."""
        self._ref = ref

    async def validate(self) -> None:
        """Validate the bridge worker."""
        await self._ref.validate()

    async def poll_workflow_activation(
        self,
    ) -> temporalio.bridge.proto.workflow_activation.WorkflowActivation:
        """Poll for a workflow activation."""
        return (
            temporalio.bridge.proto.workflow_activation.WorkflowActivation.FromString(
                await self._ref.poll_workflow_activation()
            )
        )

    async def poll_activity_task(
        self,
    ) -> temporalio.bridge.proto.activity_task.ActivityTask:
        """Poll for an activity task."""
        return temporalio.bridge.proto.activity_task.ActivityTask.FromString(
            await self._ref.poll_activity_task()
        )

    async def complete_workflow_activation(
        self,
        comp: temporalio.bridge.proto.workflow_completion.WorkflowActivationCompletion,
    ) -> None:
        """Complete a workflow activation."""
        await self._ref.complete_workflow_activation(comp.SerializeToString())

    async def complete_activity_task(
        self, comp: temporalio.bridge.proto.ActivityTaskCompletion
    ) -> None:
        """Complete an activity task."""
        await self._ref.complete_activity_task(comp.SerializeToString())

    def record_activity_heartbeat(
        self, comp: temporalio.bridge.proto.ActivityHeartbeat
    ) -> None:
        """Record an activity heartbeat."""
        self._ref.record_activity_heartbeat(comp.SerializeToString())

    def request_workflow_eviction(self, run_id: str) -> None:
        """Request a workflow be evicted."""
        self._ref.request_workflow_eviction(run_id)

    def replace_client(self, client: temporalio.bridge.client.Client) -> None:
        """Replace the worker client."""
        self._ref.replace_client(client._ref)

    def initiate_shutdown(self) -> None:
        """Start shutdown of the worker."""
        self._ref.initiate_shutdown()

    async def finalize_shutdown(self) -> None:
        """Finalize the worker.

        This will fail if shutdown hasn't completed fully due to internal
        reference count checks.
        """
        ref = self._ref
        self._ref = None
        await ref.finalize_shutdown()


# See https://mypy.readthedocs.io/en/stable/runtime_troubles.html#using-classes-that-are-generic-in-stubs-but-not-at-runtime
if TYPE_CHECKING:
    PayloadContainer: TypeAlias = (
        google.protobuf.internal.containers.RepeatedCompositeFieldContainer[
            temporalio.api.common.v1.Payload
        ]
    )
else:
    PayloadContainer: TypeAlias = (
        google.protobuf.internal.containers.RepeatedCompositeFieldContainer
    )


async def _apply_to_payloads(
    payloads: PayloadContainer,
    cb: Callable[
        [Sequence[temporalio.api.common.v1.Payload]],
        Awaitable[List[temporalio.api.common.v1.Payload]],
    ],
) -> None:
    """Apply API payload callback to payloads."""
    if len(payloads) == 0:
        return
    new_payloads = await cb(payloads)
    if new_payloads is payloads:
        return
    del payloads[:]
    # TODO(cretz): Copy too expensive?
    payloads.extend(new_payloads)


async def _apply_to_payload(
    payload: temporalio.api.common.v1.Payload,
    cb: Callable[
        [Sequence[temporalio.api.common.v1.Payload]],
        Awaitable[List[temporalio.api.common.v1.Payload]],
    ],
) -> None:
    """Apply API payload callback to payload."""
    new_payload = (await cb([payload]))[0]
    payload.CopyFrom(new_payload)


async def _decode_payloads(
    payloads: PayloadContainer,
    codec: temporalio.converter.PayloadCodec,
) -> None:
    """Decode payloads with the given codec."""
    return await _apply_to_payloads(payloads, codec.decode)


async def _decode_payload(
    payload: temporalio.api.common.v1.Payload,
    codec: temporalio.converter.PayloadCodec,
) -> None:
    """Decode a payload with the given codec."""
    return await _apply_to_payload(payload, codec.decode)


async def _encode_payloads(
    payloads: PayloadContainer,
    codec: temporalio.converter.PayloadCodec,
) -> None:
    """Encode payloads with the given codec."""
    return await _apply_to_payloads(payloads, codec.encode)


async def _encode_payload(
    payload: temporalio.api.common.v1.Payload,
    codec: temporalio.converter.PayloadCodec,
) -> None:
    """Decode a payload with the given codec."""
    return await _apply_to_payload(payload, codec.encode)


async def decode_activation(
    act: temporalio.bridge.proto.workflow_activation.WorkflowActivation,
    codec: temporalio.converter.PayloadCodec,
) -> None:
    """Decode the given activation with the codec."""
    for job in act.jobs:
        if job.HasField("cancel_workflow"):
            await _decode_payloads(job.cancel_workflow.details, codec)
        elif job.HasField("query_workflow"):
            await _decode_payloads(job.query_workflow.arguments, codec)
        elif job.HasField("resolve_activity"):
            if job.resolve_activity.result.HasField("cancelled"):
                await codec.decode_failure(
                    job.resolve_activity.result.cancelled.failure
                )
            elif job.resolve_activity.result.HasField("completed"):
                if job.resolve_activity.result.completed.HasField("result"):
                    await _decode_payload(
                        job.resolve_activity.result.completed.result, codec
                    )
            elif job.resolve_activity.result.HasField("failed"):
                await codec.decode_failure(job.resolve_activity.result.failed.failure)
        elif job.HasField("resolve_child_workflow_execution"):
            if job.resolve_child_workflow_execution.result.HasField("cancelled"):
                await codec.decode_failure(
                    job.resolve_child_workflow_execution.result.cancelled.failure
                )
            elif job.resolve_child_workflow_execution.result.HasField(
                "completed"
            ) and job.resolve_child_workflow_execution.result.completed.HasField(
                "result"
            ):
                await _decode_payload(
                    job.resolve_child_workflow_execution.result.completed.result, codec
                )
            elif job.resolve_child_workflow_execution.result.HasField("failed"):
                await codec.decode_failure(
                    job.resolve_child_workflow_execution.result.failed.failure
                )
        elif job.HasField("resolve_child_workflow_execution_start"):
            if job.resolve_child_workflow_execution_start.HasField("cancelled"):
                await codec.decode_failure(
                    job.resolve_child_workflow_execution_start.cancelled.failure
                )
        elif job.HasField("resolve_request_cancel_external_workflow"):
            if job.resolve_request_cancel_external_workflow.HasField("failure"):
                await codec.decode_failure(
                    job.resolve_request_cancel_external_workflow.failure
                )
        elif job.HasField("resolve_signal_external_workflow"):
            if job.resolve_signal_external_workflow.HasField("failure"):
                await codec.decode_failure(job.resolve_signal_external_workflow.failure)
        elif job.HasField("signal_workflow"):
            await _decode_payloads(job.signal_workflow.input, codec)
        elif job.HasField("initialize_workflow"):
            await _decode_payloads(job.initialize_workflow.arguments, codec)
            if job.initialize_workflow.HasField("continued_failure"):
                await codec.decode_failure(job.initialize_workflow.continued_failure)
            for val in job.initialize_workflow.memo.fields.values():
                # This uses API payload not bridge payload
                new_payload = (await codec.decode([val]))[0]
                val.metadata.clear()
                val.metadata.update(new_payload.metadata)
                val.data = new_payload.data
        elif job.HasField("do_update"):
            await _decode_payloads(job.do_update.input, codec)


async def encode_completion(
    comp: temporalio.bridge.proto.workflow_completion.WorkflowActivationCompletion,
    codec: temporalio.converter.PayloadCodec,
) -> None:
    """Recursively encode the given completion with the codec."""
    if comp.HasField("failed"):
        await codec.encode_failure(comp.failed.failure)
    elif comp.HasField("successful"):
        for command in comp.successful.commands:
            if command.HasField("complete_workflow_execution"):
                if command.complete_workflow_execution.HasField("result"):
                    await _encode_payload(
                        command.complete_workflow_execution.result, codec
                    )
            elif command.HasField("continue_as_new_workflow_execution"):
                await _encode_payloads(
                    command.continue_as_new_workflow_execution.arguments, codec
                )
                for val in command.continue_as_new_workflow_execution.memo.values():
                    await _encode_payload(val, codec)
            elif command.HasField("fail_workflow_execution"):
                await codec.encode_failure(command.fail_workflow_execution.failure)
            elif command.HasField("respond_to_query"):
                if command.respond_to_query.HasField("failed"):
                    await codec.encode_failure(command.respond_to_query.failed)
                elif command.respond_to_query.HasField(
                    "succeeded"
                ) and command.respond_to_query.succeeded.HasField("response"):
                    await _encode_payload(
                        command.respond_to_query.succeeded.response, codec
                    )
            elif command.HasField("schedule_activity"):
                await _encode_payloads(command.schedule_activity.arguments, codec)
            elif command.HasField("schedule_local_activity"):
                await _encode_payloads(command.schedule_local_activity.arguments, codec)
            elif command.HasField("signal_external_workflow_execution"):
                await _encode_payloads(
                    command.signal_external_workflow_execution.args, codec
                )
            elif command.HasField("start_child_workflow_execution"):
                await _encode_payloads(
                    command.start_child_workflow_execution.input, codec
                )
                for val in command.start_child_workflow_execution.memo.values():
                    await _encode_payload(val, codec)
            elif command.HasField("update_response"):
                if command.update_response.HasField("completed"):
                    await _encode_payload(command.update_response.completed, codec)
                elif command.update_response.HasField("rejected"):
                    await codec.encode_failure(command.update_response.rejected)
