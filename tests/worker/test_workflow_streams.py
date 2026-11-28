"""Tests of the Stream calls of one Workflow instance, with no server."""

import asyncio
import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from google.protobuf.duration_pb2 import Duration
from google.protobuf.message import Message

import temporalio.api.common.v1
import temporalio.bridge.worker
import temporalio.common
import temporalio.converter
import temporalio.converter._extstore
import temporalio.nexus.system
import temporalio.worker
from temporalio import workflow
from temporalio.api.common.v1 import ActivityType, Payload
from temporalio.api.enums.v1 import RetryState, StreamErrorReason, TimeoutType
from temporalio.api.failure.v1 import (
    ActivityFailureInfo,
    ApplicationFailureInfo,
    Failure,
    TimeoutFailureInfo,
)
from temporalio.api.sdk.v1 import (
    CreateStreamRequest,
    CreateStreamResponse,
    DescribeStreamRequest,
    StreamRef,
)
from temporalio.api.sdk.v1.external_storage_pb2 import ExternalStorageReference
from temporalio.api.stream.v1 import StreamRecord
from temporalio.api.streamservice.v1 import DescribeStreamResponse
from temporalio.bridge.proto.activity_result import (
    ActivityResolution,
)
from temporalio.bridge.proto.activity_result import Success as ActivitySuccess
from temporalio.bridge.proto.workflow_activation import (
    InitializeWorkflow,
    RemoveFromCache,
    ResolveActivity,
    ResolveStreamAppend,
    ResolveStreamRead,
    SignalWorkflow,
    StreamAppended,
    StreamPage,
    WorkflowActivation,
    WorkflowActivationJob,
)
from temporalio.bridge.proto.workflow_commands import (
    AppendToStream,
    CompleteWorkflowExecution,
    ReadStream,
    RequestCancelLocalActivity,
    WorkflowCommand,
)
from temporalio.bridge.proto.workflow_completion import (
    Success,
    WorkflowActivationCompletion,
)
from temporalio.client import StreamConfig, StreamSelectContext, StreamStore
from temporalio.converter import (
    ExternalStorage,
    StorageDriverWorkflowInfo,
    WorkflowSerializationContext,
)
from temporalio.exceptions import ApplicationError, StreamError
from temporalio.nexus.system import _support
from temporalio.nexus.system.stream_service import StreamRef as ModelRef
from temporalio.nexus.system.stream_service.models import (
    CreateStreamResult,
    StreamIdConflictPolicy,
)
from temporalio.testing import MemoryStreamStore
from temporalio.worker._workflow import _CommandAwareDataConverter
from temporalio.worker._workflow_instance import (
    _WorkflowInstanceImpl,
    _WorkflowLogicFlag,  # type: ignore[reportPrivateLocalImportUsage]
)
from tests.helpers.streams import (
    CodecCall,
    ContextCodec,
    core_failure,
    failure_note,
    wrapped,
)
from tests.test_extstore import InMemoryTestDriver


@workflow.defn
class _StreamWorkflow:
    @workflow.run
    async def run(self) -> None:
        pass


def _new_instance(
    workflow_class: type,
    stream_config: StreamConfig | None = None,
    interceptor_classes: Sequence[
        type[temporalio.worker.WorkflowInboundInterceptor]
    ] = (),
    failure_exception_types: Sequence[type[BaseException]] = (),
) -> _WorkflowInstanceImpl:
    defn = workflow._Definition.must_from_class(workflow_class)
    start_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    instance = temporalio.worker.UnsandboxedWorkflowRunner().create_instance(
        temporalio.worker.WorkflowInstanceDetails(
            payload_converter_factory=temporalio.converter.DefaultPayloadConverter,
            failure_converter_class=temporalio.converter.DefaultFailureConverter,
            interceptor_classes=interceptor_classes,
            defn=defn,
            info=workflow.Info(
                attempt=1,
                continued_run_id=None,
                cron_schedule=None,
                execution_timeout=None,
                first_execution_run_id="first-run",
                headers={},
                namespace="ns",
                original_execution_run_id="run",
                parent=None,
                root=None,
                priority=temporalio.common.Priority(),
                raw_memo={},
                retry_policy=None,
                run_id="run",
                run_timeout=None,
                search_attributes={},
                start_time=start_time,
                task_queue="tq",
                task_timeout=timedelta(seconds=10),
                typed_search_attributes=temporalio.common.TypedSearchAttributes.empty,
                workflow_id="wf",
                workflow_start_time=start_time,
                workflow_type=defn.name or "",
            ),
            randomness_seed=1,
            extern_functions={"__temporal_stream_config": lambda: stream_config},
            disable_eager_activity_execution=False,
            worker_level_failure_exception_types=failure_exception_types,
            patch_activation_callback=None,
            last_completion_result=temporalio.api.common.v1.Payloads(),
            last_failure=None,
        )
    )
    assert isinstance(instance, _WorkflowInstanceImpl)
    return instance


async def _activate(
    instance: _WorkflowInstanceImpl,
    *jobs: WorkflowActivationJob,
    replaying: bool = False,
    flags: Sequence[int] = (),
) -> WorkflowActivationCompletion:
    # A Worker runs an activation in another thread too, so that the tasks of the
    # Workflow don't run inside the task of the caller.
    activation = WorkflowActivation(
        run_id="run", jobs=jobs, is_replaying=replaying, available_internal_flags=flags
    )
    return await asyncio.to_thread(instance.activate, activation)


async def test_stream_payloads_take_the_codec_with_no_context_and_no_storage() -> None:
    calls: list[CodecCall] = []
    driver = InMemoryTestDriver()
    converter = dataclasses.replace(
        temporalio.converter.DataConverter.default,
        payload_codec=ContextCodec(calls),
        external_storage=ExternalStorage(drivers=[driver], payload_size_threshold=0),
    )
    context = WorkflowSerializationContext(namespace="ns", workflow_id="wf")
    command_aware = _CommandAwareDataConverter.create(
        instance=_new_instance(_StreamWorkflow),
        context_free_dc=converter,
        workflow_context_dc=converter.with_context(context),
        workflow_context=context,
    )
    to_payload = temporalio.converter.PayloadConverter.default.to_payload
    reference = to_payload(ExternalStorageReference(driver_name=driver.name()))
    bodies = [to_payload("plain"), reference]
    result = to_payload("result")

    completion = WorkflowActivationCompletion(
        successful=Success(
            commands=[
                WorkflowCommand(
                    append_to_stream=AppendToStream(
                        seq=1, stream=StreamRef(id="s", store="memory"), bodies=bodies
                    )
                ),
                WorkflowCommand(
                    complete_workflow_execution=CompleteWorkflowExecution(result=result)
                ),
            ]
        )
    )
    await temporalio.bridge.worker.encode_completion(
        completion, command_aware, encode_headers=True, storage_concurrency_limit=1
    )
    append, complete = completion.successful.commands
    assert list(append.append_to_stream.bodies) == [wrapped(body) for body in bodies]
    stored = complete.complete_workflow_execution.result
    assert stored.metadata["messageType"] == reference.metadata["messageType"]
    assert driver._store_calls == 1
    assert calls == [("encode", None, 1)] * 2 + [("encode", context, 1)]

    # A writer with no codec can write a claim of a payload in this Worker's storage, and
    # a body that has the mark of a system Nexus envelope.
    claim = Payload()
    claim.CopyFrom(stored)
    claim.metadata[temporalio.converter._extstore._REFERENCE_MARKER_KEY] = b"true"
    envelope = to_payload("envelope")
    envelope.metadata[temporalio.nexus.system._SYSTEM_PAYLOAD_METADATA_KEY] = b"true"
    failure = Failure(
        message="A later attempt has written to the Stream",
        application_failure_info=ApplicationFailureInfo(
            type="stream_fenced",
            details=temporalio.api.common.v1.Payloads(payloads=[wrapped(result)]),
        ),
    )
    records = [*append.append_to_stream.bodies, claim, envelope]
    activation = WorkflowActivation(
        jobs=[
            WorkflowActivationJob(
                resolve_stream_read=ResolveStreamRead(
                    seq=1,
                    page=StreamPage(
                        records=[
                            StreamRecord(body=body, offset=bytes([at + 1]))
                            for at, body in enumerate(records)
                        ]
                    ),
                )
            ),
            WorkflowActivationJob(
                resolve_stream_read=ResolveStreamRead(seq=2, failed=failure)
            ),
            WorkflowActivationJob(
                resolve_stream_append=ResolveStreamAppend(seq=1, failed=failure)
            ),
            WorkflowActivationJob(
                resolve_activity=ResolveActivity(
                    seq=1,
                    result=ActivityResolution(completed=ActivitySuccess(result=stored)),
                )
            ),
        ]
    )
    calls.clear()
    await temporalio.bridge.worker.decode_activation(
        activation, command_aware, decode_headers=True, storage_concurrency_limit=1
    )
    read, failed_read, appended, activity = activation.jobs
    assert [record.body for record in read.resolve_stream_read.page.records] == [
        *bodies,
        claim,
        envelope,
    ]
    assert failed_read.resolve_stream_read.failed == failure
    assert appended.resolve_stream_append.failed == failure
    assert activity.resolve_activity.result.completed.result == result
    assert driver._retrieve_calls == 1
    assert calls == [("decode", None, 1)] * 4 + [("decode", context, 1)]


_REFUSALS = [
    StreamErrorReason.STREAM_ERROR_REASON_NOT_FOUND,
    StreamErrorReason.STREAM_ERROR_REASON_ALREADY_EXISTS,
    StreamErrorReason.STREAM_ERROR_REASON_EXPIRED,
    StreamErrorReason.STREAM_ERROR_REASON_FENCED,
]
_UNAVAILABLE = StreamErrorReason.STREAM_ERROR_REASON_STORE_UNAVAILABLE
_STORE_CALL = "__temporal_stream_store_call"


@dataclass
class _Call:
    operation: str
    stream: str
    store: str | None = None
    bodies: Sequence[int] = ()
    cancel: bool = False
    refusal: StreamErrorReason.ValueType | None = None
    timeout: bool = False
    unserved: bool = False
    outcome: Any = None


class _Script:
    phases: list[list[_Call]] = []
    late_cancels = False
    intercepted: list[tuple[str, Any]] = []
    local_activities: list[str] = []


@workflow.defn
class _ScriptWorkflow:
    @workflow.run
    async def run(self) -> None:
        streams: dict[str, Any] = {}
        for phase in _Script.phases:
            tasks = [asyncio.ensure_future(self._call(streams, call)) for call in phase]
            # Each task makes its call in its first step, and the task of the call runs
            # one step later.
            await asyncio.sleep(0)
            if _Script.late_cancels:
                await asyncio.sleep(0)
            for call, task in zip(phase, tasks):
                if call.cancel:
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _call(self, streams: dict[str, Any], call: _Call) -> None:
        try:
            if call.operation == "create":
                stream = await workflow.create_stream(
                    id=call.stream, store=call.store, body_type=int
                )
                streams[call.stream] = stream
                call.outcome = (stream.ref.store, stream.created)
                return
            stream = streams.get(call.stream) or workflow.get_stream(
                ModelRef[int](id=call.stream, store=call.store)
            )
            if call.operation == "append":
                appended = await stream.append(*call.bodies)
                call.outcome = (appended.first_offset, appended.last_offset)
            else:
                described = await stream.describe()
                call.outcome = described.last_offset
        except StreamError as err:
            call.outcome = err.reason
        except asyncio.CancelledError:
            call.outcome = "cancelled"
            raise


class _RecordingInbound(temporalio.worker.WorkflowInboundInterceptor):
    def init(self, outbound: temporalio.worker.WorkflowOutboundInterceptor) -> None:
        super().init(_RecordingOutbound(outbound))


class _RecordingOutbound(temporalio.worker.WorkflowOutboundInterceptor):
    def start_local_activity(
        self, input: temporalio.worker.StartLocalActivityInput
    ) -> workflow.ActivityHandle[Any]:
        _Script.local_activities.append(input.activity)
        return super().start_local_activity(input)

    async def start_create_stream(self, request: Any, output_type: Any = None) -> Any:
        _Script.intercepted.append(("create", request))
        return await super().start_create_stream(request, output_type)

    async def start_append_records(self, request: Any) -> Any:
        _Script.intercepted.append(("append", request))
        return await super().start_append_records(request)

    async def start_describe_stream(self, request: Any) -> Any:
        _Script.intercepted.append(("describe", request))
        return await super().start_describe_stream(request)


def _resolve_call(
    call: _Call, seq: int, *, created: bool = True
) -> WorkflowActivationJob:
    converter = temporalio.converter.DefaultPayloadConverter()
    job = WorkflowActivationJob()
    if call.operation == "append":
        job.resolve_stream_append.seq = seq
        if call.refusal is not None:
            job.resolve_stream_append.failed.CopyFrom(core_failure(call.refusal))
        else:
            job.resolve_stream_append.appended.CopyFrom(
                StreamAppended(
                    first_offset=b"first%d" % seq, last_offset=b"last%d" % seq
                )
            )
        return job

    job.resolve_activity.seq = seq
    job.resolve_activity.is_local = True
    result = job.resolve_activity.result
    if call.cancel:
        result.cancelled.failure.canceled_failure_info.SetInParent()
    elif call.refusal is not None:
        result.failed.failure.CopyFrom(core_failure(call.refusal))
    elif call.unserved:
        result.failed.failure.CopyFrom(
            Failure(
                message="No Stream Store",
                application_failure_info=ApplicationFailureInfo(type="ValueError"),
            )
        )
    elif call.timeout:
        result.failed.failure.CopyFrom(
            Failure(
                message="Local Activity timed out",
                cause=Failure(
                    timeout_failure_info=TimeoutFailureInfo(
                        timeout_type=TimeoutType.TIMEOUT_TYPE_SCHEDULE_TO_CLOSE
                    )
                ),
                activity_failure_info=ActivityFailureInfo(
                    activity_type=ActivityType(name=_STORE_CALL),
                    retry_state=RetryState.RETRY_STATE_CANCEL_REQUESTED,
                ),
            )
        )
    else:
        answer: Message = (
            CreateStreamResponse(
                ref=StreamRef(id=call.stream, store="b"), created=created
            )
            if call.operation == "create"
            else DescribeStreamResponse(last_offset=b"last%d" % seq)
        )
        result.completed.result.CopyFrom(
            converter.to_payload(answer.SerializeToString())
        )
    return job


@workflow.defn
class _ReuseStreamWorkflow:
    @workflow.run
    async def run(self) -> bool | None:
        stream = await workflow.create_stream(
            id="s",
            store="b",
            id_conflict_policy=StreamIdConflictPolicy.StreamIdConflictPolicyUseExisting,
        )
        return stream.created


@pytest.mark.parametrize("intercepted", [False, True])
async def test_create_preserves_the_recorded_existing_stream_result(
    intercepted: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def existing(_self: Any, request: Any, _output_type: Any = None) -> Any:
        answer = asyncio.get_running_loop().create_future()
        answer.set_result(CreateStreamResult(ref=request.stream_ref, created=False))
        return answer

    if intercepted:
        monkeypatch.setattr(_RecordingOutbound, "start_create_stream", existing)
    flag = _WorkflowLogicFlag.REPORT_EXISTING_STREAM_AS_FALSE
    for replaying, flags, expected in [
        (False, (), False),
        (True, (), None),
        (True, (flag,), False),
    ]:
        instance = _new_instance(
            _ReuseStreamWorkflow,
            StreamConfig([MemoryStreamStore(name="b")]),
            [_RecordingInbound],
        )
        completion = await _activate(
            instance,
            WorkflowActivationJob(initialize_workflow=InitializeWorkflow()),
            replaying=replaying,
            flags=flags,
        )
        if not intercepted:
            [schedule] = completion.successful.commands
            completion = await _activate(
                instance,
                _resolve_call(
                    _Call("create", "s"),
                    schedule.schedule_local_activity.seq,
                    created=False,
                ),
                replaying=replaying,
                flags=flags,
            )
        [command] = completion.successful.commands
        assert command.HasField("complete_workflow_execution")
        payload = command.complete_workflow_execution.result
        assert (
            temporalio.converter.PayloadConverter.default.from_payload(payload)
            is expected
        )
        assert (flag in completion.successful.used_internal_flags) == (not replaying)


@pytest.mark.parametrize("replaying", [False, True])
@pytest.mark.parametrize("late_cancels", [False, True])
async def test_stream_calls_keep_their_results_when_jobs_arrive_in_reverse_order(
    replaying: bool, late_cancels: bool
) -> None:
    a, b = MemoryStreamStore(name="a"), MemoryStreamStore(name="b")
    selects: list[StreamSelectContext] = []

    def selector(context: StreamSelectContext) -> StreamStore:
        selects.append(context)
        return a

    phases = [
        [
            _Call("create", "created"),
            _Call("append", "appended", "a", [1, 2]),
            _Call("describe", "described", "a"),
        ],
        [
            _Call("append", "created", bodies=[3]),
            _Call("create", "cancelled", cancel=True),
            _Call("append", "cancelled-append", "a", [4], cancel=True),
            _Call("describe", "refused", "a", refusal=_REFUSALS[0]),
        ],
    ]
    _Script.phases, _Script.intercepted, _Script.local_activities = phases, [], []
    _Script.late_cancels = late_cancels
    instance = _new_instance(
        _ScriptWorkflow,
        StreamConfig([a, b], store_selector=selector),
        [_RecordingInbound],
    )
    converter = temporalio.converter.DefaultPayloadConverter()
    jobs = [WorkflowActivationJob(initialize_workflow=InitializeWorkflow())]
    seqs = {"append": 0, "activity": 0}
    for phase in phases:
        completion = await _activate(instance, *jobs, replaying=replaying)
        commands = completion.successful.commands
        jobs = []
        cancelled: list[int] = []
        for call, command in zip(phase, commands[: len(phase)], strict=True):
            kind = "append" if call.operation == "append" else "activity"
            seqs[kind] += 1
            seq = seqs[kind]
            if kind == "append":
                assert command.append_to_stream.seq == seq
                assert command.append_to_stream.stream == StreamRef(
                    id=call.stream, store="b" if call.stream == "created" else "a"
                )
                assert converter.from_payloads(command.append_to_stream.bodies) == list(
                    call.bodies
                )
            else:
                activity = command.schedule_local_activity
                assert activity.seq == seq and activity.activity_type == _STORE_CALL
                operation, store, raw, request_id = converter.from_payloads(
                    activity.arguments
                )
                assert operation == (
                    "CreateStream" if call.operation == "create" else "DescribeStream"
                )
                assert store == "a"
                assert request_id == (
                    "first-run" if call.operation == "create" else None
                )
                if call.operation == "create":
                    assert CreateStreamRequest.FromString(raw).stream_ref == StreamRef(
                        id=call.stream
                    )
                else:
                    assert DescribeStreamRequest.FromString(raw).ref == StreamRef(
                        id=call.stream, store="a"
                    )
                if call.cancel:
                    cancelled.append(seq)
            jobs.append(_resolve_call(call, seq))
        assert list(commands[len(phase) :]) == [
            WorkflowCommand(
                request_cancel_local_activity=RequestCancelLocalActivity(seq=seq)
            )
            for seq in cancelled
        ]
        jobs.reverse()
    completion = await _activate(instance, *jobs, replaying=replaying)
    [command] = completion.successful.commands
    assert command.HasField("complete_workflow_execution")
    calls = [call for phase in phases for call in phase]
    assert [call.outcome for call in calls] == [
        ("b", True),
        (b"first1", b"last1"),
        b"last2",
        (b"first2", b"last2"),
        "cancelled",
        "cancelled",
        _REFUSALS[0],
    ]
    assert not instance._streams._appends
    target = StorageDriverWorkflowInfo(
        namespace="ns", id="wf", run_id="run", type="_ScriptWorkflow"
    )
    assert selects == [
        StreamSelectContext(id=id, target=target, namespace="ns")
        for id in ("created", "cancelled")
    ]
    assert [operation for operation, _ in _Script.intercepted] == [
        call.operation for call in calls
    ]
    assert _Script.local_activities == [_STORE_CALL] * 4
    for operation, request in _Script.intercepted:
        if operation == "create":
            assert request.stream_ref.store is None
    assert (
        await _activate(
            instance, WorkflowActivationJob(remove_from_cache=RemoveFromCache())
        )
    ).HasField("successful")


@pytest.mark.parametrize("replaying", [False, True])
@pytest.mark.parametrize(
    ("operation", "failure"),
    [
        (op, failure)
        for op in ("create", "describe")
        for failure in ("outage", "timeout", "unserved")
    ]
    + [(op, "selector") for op in ("create", "append", "describe")],
)
async def test_store_failures_fail_the_task_even_when_workflow_errors_are_enabled(
    operation: str, failure: str, replaying: bool
) -> None:
    store = MemoryStreamStore(name="a")

    def selector(_context: StreamSelectContext) -> StreamStore:
        if failure == "selector":
            raise ApplicationError("The selector has no Store for this Stream")
        return store

    call = _Call(
        operation,
        "s",
        bodies=[1],
        refusal=_UNAVAILABLE if failure == "outage" else None,
        timeout=failure == "timeout",
        unserved=failure == "unserved",
    )
    _Script.phases, _Script.late_cancels = [[call]], False
    instance = _new_instance(
        _ScriptWorkflow,
        StreamConfig([store], store_selector=selector),
        failure_exception_types=[Exception],
    )
    completion = await _activate(
        instance,
        WorkflowActivationJob(initialize_workflow=InitializeWorkflow()),
        replaying=replaying,
    )
    if failure != "selector":
        assert len(completion.successful.commands) == 1
        completion = await _activate(
            instance, _resolve_call(call, 1), replaying=replaying
        )
    assert completion.HasField("failed")
    assert (
        await _activate(
            instance, WorkflowActivationJob(remove_from_cache=RemoveFromCache())
        )
    ).HasField("successful")


class _EachCall:
    """The calls of :py:class:`_EachCallWorkflow`, and what its selector saw."""

    operations: list[str] = []
    instance: _WorkflowInstanceImpl | None = None
    selects: list[tuple[str, int]] = []
    """The Stream id of each selector call, with the number of commands before it."""


@workflow.defn
class _EachCallWorkflow:
    @workflow.run
    async def run(self) -> None:
        calls: list[Any] = []
        for at, operation in enumerate(_EachCall.operations):
            stream = workflow.get_stream(ModelRef[int](id=f"s{at}"))
            if operation == "create":
                calls.append(workflow.create_stream(id=f"s{at}", body_type=int))
            elif operation == "append":
                calls.append(stream.append(at))
            elif operation == "read":
                calls.append(stream.read_page())
            else:
                calls.append(stream.describe())
        if len(calls) == 1:
            # The run gets the error of its one call, so no task holds an error that
            # no one reads.
            await calls[0]
            return
        tasks = [asyncio.ensure_future(call) for call in calls]
        # Each task makes its call in its first step.
        await asyncio.sleep(0)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _store_of(command: WorkflowCommand) -> str:
    """Return the Store name that the command of a Stream call has."""
    variant = command.WhichOneof("variant")
    if variant == "append_to_stream":
        return command.append_to_stream.stream.store
    if variant == "read_stream":
        return command.read_stream.stream.store
    # The Local Activity of a create or a describe has the Store name second.
    [_, store, *_] = command.schedule_local_activity.arguments
    return temporalio.converter.DefaultPayloadConverter().from_payload(store, str)


async def test_the_store_of_a_call_is_chosen_before_its_command() -> None:
    operations = ["create", "append", "read", "describe"]
    a, b = MemoryStreamStore(name="a"), MemoryStreamStore(name="b")
    for on_b in ([False, True, False, True], [True, False, True, False]):

        def selector(context: StreamSelectContext) -> StreamStore:
            assert _EachCall.instance
            sent = _EachCall.instance._current_completion.successful.commands
            _EachCall.selects.append((context.id, len(sent)))
            return b if on_b[int(context.id[1:])] else a

        config = StreamConfig([a, b], store_selector=selector)
        _EachCall.operations = operations
        for replaying in (False, True):
            with failure_note(f"on b {on_b}, replaying {replaying}"):
                _EachCall.selects = []
                instance = _EachCall.instance = _new_instance(_EachCallWorkflow, config)
                initialize = WorkflowActivationJob(
                    initialize_workflow=InitializeWorkflow()
                )
                completion = await _activate(instance, initialize, replaying=replaying)
                # The selector runs once for each call, before the command of the call.
                assert _EachCall.selects == [
                    (f"s{at}", at) for at in range(len(operations))
                ]
                commands = completion.successful.commands
                assert [command.WhichOneof("variant") for command in commands] == [
                    "schedule_local_activity",
                    "append_to_stream",
                    "read_stream",
                    "schedule_local_activity",
                    # A cancel sends a command only for a Local Activity.
                    "request_cancel_local_activity",
                    "request_cancel_local_activity",
                    "complete_workflow_execution",
                ]
                assert [_store_of(command) for command in commands[:4]] == [
                    "b" if mine else "a" for mine in on_b
                ]


# SDK Core uses the only Store of a Worker for a ref with no Store name. The SDK
# picks that Store itself, so each command has its name.
async def test_a_ref_with_no_store_name_goes_to_the_only_store() -> None:
    operations = ["create", "append", "read", "describe"]
    config = StreamConfig([MemoryStreamStore(name="only")])
    _EachCall.operations = operations
    for replaying in (False, True):
        with failure_note(f"replaying {replaying}"):
            instance = _new_instance(_EachCallWorkflow, config)
            initialize = WorkflowActivationJob(initialize_workflow=InitializeWorkflow())
            completion = await _activate(instance, initialize, replaying=replaying)
            commands = completion.successful.commands[: len(operations)]
            assert [command.WhichOneof("variant") for command in commands] == [
                "schedule_local_activity",
                "append_to_stream",
                "read_stream",
                "schedule_local_activity",
            ]
            stores = [_store_of(command) for command in commands]
            assert stores == ["only"] * len(operations)


async def test_a_ref_with_no_store_name_needs_a_selector_for_two_stores() -> None:
    a, b = MemoryStreamStore(name="a"), MemoryStreamStore(name="b")
    with pytest.raises(ValueError, match="store_selector must be specified"):
        StreamConfig([a, b])
    unregistered = MemoryStreamStore(name="c")
    config = StreamConfig([a, b], store_selector=lambda _: unregistered)
    for operation in ["create", "append", "read", "describe"]:
        _EachCall.operations = [operation]
        for replaying in (False, True):
            with failure_note(f"{operation}, replaying {replaying}"):
                instance = _new_instance(_EachCallWorkflow, config)
                initialize = WorkflowActivationJob(
                    initialize_workflow=InitializeWorkflow()
                )
                completion = await _activate(instance, initialize, replaying=replaying)
                message = completion.failed.failure.message
                assert "not registered in StreamConfig.stores" in message


async def test_a_stream_call_with_no_stream_config_fails_the_task() -> None:
    for operation in ["create", "append", "read", "describe"]:
        _EachCall.operations = [operation]
        for replaying in (False, True):
            instance = _new_instance(_EachCallWorkflow)
            initialize = WorkflowActivationJob(initialize_workflow=InitializeWorkflow())
            completion = await _activate(instance, initialize, replaying=replaying)
            assert "no stream_config" in completion.failed.failure.message, operation


@dataclass
class _ReadCall:
    stream: str
    start: int
    """The offset that the first page comes after. Zero is the start of the Stream."""
    wait: timedelta | None
    max_records: int | None
    max_bytes: int | None
    pages: int
    """The maximum number of pages that the reader asks for."""
    body_type: Any
    """The body type of the reader's Stream, which is ``int``, ``Any`` or none."""
    outcome: list[Any] = dataclasses.field(default_factory=list)


class _ReadScript:
    calls: list[_ReadCall] = []
    steps: list[tuple[list[int], list[int]]] = []


@workflow.defn
class _ReadsWorkflow:
    def __init__(self) -> None:
        self._readers: dict[int, asyncio.Future[None]] = {}

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)

    @workflow.signal
    def step(self) -> None:
        cancels, starts = _ReadScript.steps.pop(0)
        for call in cancels:
            self._readers[call].cancel()
        for call in starts:
            reader = self._read(_ReadScript.calls[call])
            self._readers[call] = asyncio.ensure_future(reader)

    async def _read(self, call: _ReadCall) -> None:
        body_type: Any = call.body_type
        ref: Any = ModelRef(id=call.stream, store="a")
        if body_type:
            ref = ModelRef[body_type](id=call.stream, store="a")
        stream = workflow.get_stream(ref)
        offset = _offset(call.start) or None
        try:
            for _ in range(call.pages):
                page = await stream.read_page(
                    from_offset=offset,
                    wait=call.wait,
                    max_records=call.max_records,
                    max_bytes=call.max_bytes,
                )
                bodies = [record.body for record in page.records]
                offset = page.last_offset
                call.outcome.append((bodies, offset or b""))
        except StreamError as err:
            call.outcome.append(err.reason)
        except TypeError:
            call.outcome.append("TypeError")
        except asyncio.CancelledError:
            call.outcome.append("cancelled")
            raise


def _offset(number: int) -> bytes:
    return number.to_bytes(8, "big") if number else b""


_READ_FLAGS = [
    (),
    (_WorkflowLogicFlag.PROCESS_WORKFLOW_ACTIVATION_JOBS_AS_SINGLE_BATCH,),
]


async def _start_readers(
    calls: list[_ReadCall], flags: Sequence[int], replaying: bool
) -> tuple[_WorkflowInstanceImpl, WorkflowActivationCompletion]:
    _ReadScript.calls = calls
    _ReadScript.steps = [([], list(range(len(calls))))]
    instance = _new_instance(
        _ReadsWorkflow, StreamConfig([MemoryStreamStore(name="a")])
    )
    completion = await _activate(
        instance,
        WorkflowActivationJob(initialize_workflow=InitializeWorkflow()),
        WorkflowActivationJob(signal_workflow=SignalWorkflow(signal_name="step")),
        flags=flags,
        replaying=replaying,
    )
    return instance, completion


def _read_result(seq: int, bodies: Sequence[Any], last: int) -> WorkflowActivationJob:
    converter = temporalio.converter.DefaultPayloadConverter()
    return WorkflowActivationJob(
        resolve_stream_read=ResolveStreamRead(
            seq=seq,
            page=StreamPage(
                records=[
                    StreamRecord(offset=_offset(at), body=converter.to_payload(body))
                    for at, body in enumerate(bodies, 1)
                ],
                last_offset=_offset(last),
            ),
        )
    )


@pytest.mark.parametrize("flags", _READ_FLAGS)
@pytest.mark.parametrize("replaying", [False, True])
async def test_read_pages_keep_their_sequence_and_cursor(
    flags: Sequence[int], replaying: bool
) -> None:
    calls = [
        _ReadCall("first", 1, timedelta(seconds=5), 2, 100, 2, int),
        _ReadCall("second", 0, timedelta(0), None, None, 1, Any),
        _ReadCall("untyped", 0, None, None, None, 1, None),
    ]
    instance, completion = await _start_readers(calls, flags, replaying)
    expected = [
        ReadStream(
            seq=1,
            stream=StreamRef(id="first", store="a"),
            from_offset=_offset(1),
            wait=Duration(seconds=5),
            max_records=2,
            max_bytes=100,
        ),
        ReadStream(seq=2, stream=StreamRef(id="second", store="a"), wait=Duration()),
    ]
    assert list(completion.successful.commands) == [
        WorkflowCommand(read_stream=read) for read in expected
    ]
    assert calls[2].outcome == ["TypeError"]

    completion = await _activate(
        instance,
        _read_result(2, ["text"], 3),
        _read_result(1, [], 4),
        flags=flags,
        replaying=replaying,
    )
    assert [call.outcome for call in calls] == [
        [([], _offset(4))],
        [(["text"], _offset(3))],
        ["TypeError"],
    ]
    next_read = expected[0]
    next_read.seq, next_read.from_offset = 3, _offset(4)
    assert list(completion.successful.commands) == [
        WorkflowCommand(read_stream=next_read)
    ]

    reason = StreamErrorReason.STREAM_ERROR_REASON_NOT_FOUND
    completion = await _activate(
        instance,
        WorkflowActivationJob(
            resolve_stream_read=ResolveStreamRead(seq=3, failed=core_failure(reason))
        ),
        flags=flags,
        replaying=replaying,
    )
    assert not completion.successful.commands
    assert calls[0].outcome == [([], _offset(4)), reason]
    assert not instance._streams._reads
    assert not (
        await _activate(
            instance, WorkflowActivationJob(remove_from_cache=RemoveFromCache())
        )
    ).successful.commands


@pytest.mark.parametrize("flags", _READ_FLAGS)
@pytest.mark.parametrize("replaying", [False, True])
@pytest.mark.parametrize("when", ["before result", "with result", "after a page"])
async def test_cancelled_reads_discard_later_results(
    flags: Sequence[int], replaying: bool, when: str
) -> None:
    call = _ReadCall("s", 0, None, None, None, 2, int)
    instance, _ = await _start_readers([call], flags, replaying)
    seq = 1
    expected: list[Any] = []
    if when == "after a page":
        await _activate(
            instance, _read_result(1, [1], 1), flags=flags, replaying=replaying
        )
        expected.append(([1], _offset(1)))
        seq = 2
    _ReadScript.steps = [([0], [])]
    cancel = WorkflowActivationJob(signal_workflow=SignalWorkflow(signal_name="step"))
    if when != "with result":
        completion = await _activate(instance, cancel, flags=flags, replaying=replaying)
        assert not completion.successful.commands
    completion = await _activate(
        instance,
        *([cancel] if when == "with result" else []),
        _read_result(seq, [2] if when == "with result" else ["not an integer"], 2),
        flags=flags,
        replaying=replaying,
    )
    assert not completion.successful.commands
    assert call.outcome == [*expected, "cancelled"]
    assert not instance._streams._reads
    assert not (
        await _activate(
            instance, WorkflowActivationJob(remove_from_cache=RemoveFromCache())
        )
    ).successful.commands


@pytest.mark.parametrize("flags", _READ_FLAGS)
@pytest.mark.parametrize("replaying", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_invalid_read_bodies_follow_recorded_activation_order(
    flags: Sequence[int], replaying: bool, cancelled: bool
) -> None:
    call = _ReadCall("s", 0, None, None, None, 1, int)
    instance, _ = await _start_readers([call], flags, replaying)
    jobs = []
    if cancelled:
        _ReadScript.steps = [([0], [])]
        jobs.append(
            WorkflowActivationJob(signal_workflow=SignalWorkflow(signal_name="step"))
        )
    jobs.append(_read_result(1, ["not an integer"], 1))
    completion = await _activate(instance, *jobs, flags=flags, replaying=replaying)
    # Single-batch Histories convert the body before the Signal can cancel its reader.
    failed = not cancelled or bool(flags)
    assert completion.HasField("failed") == failed
    assert call.outcome == ([] if failed else ["cancelled"])
    assert not instance._streams._reads
    assert not (
        await _activate(
            instance, WorkflowActivationJob(remove_from_cache=RemoveFromCache())
        )
    ).successful.commands


@workflow.defn
class _ReadToTwoWorkflow:
    @workflow.run
    async def run(self, how: str, seconds: float | None = 5) -> list[Any]:
        stream = workflow.get_stream(ModelRef[int](id="s", store="a"))
        wait = None if seconds is None else timedelta(seconds=seconds)
        if how == "read_page":
            page = await stream.read_page(wait=wait)
            return [page.records, page.last_offset]
        # A Stream has no end, so the reader stops at the writer's last Record, 2.
        found: list[Any] = []
        if how == "read":
            async for record in stream.read(wait=wait):
                found.append(record.body)
                if record.body == 2:
                    break
            return found
        async for page in stream.read_pages(wait=wait):
            found.append([record.body for record in page.records])
            if 2 in found[-1]:
                break
        return found


async def test_a_read_goes_on_after_a_page_with_no_record() -> None:
    converter = temporalio.converter.DefaultPayloadConverter()

    def read(seq: int, after: int, seconds: int | None = 5) -> WorkflowCommand:
        command = ReadStream(
            seq=seq,
            stream=StreamRef(id="s", store="a"),
            from_offset=_offset(after),
            wait=None if seconds is None else Duration(seconds=seconds),
        )
        return WorkflowCommand(read_stream=command)

    def page(
        seq: int, records: Sequence[int], last: int, wait_cut: bool = False
    ) -> WorkflowActivationJob:
        stream_page = StreamPage(
            records=[
                StreamRecord(offset=_offset(number), body=converter.to_payload(number))
                for number in records
            ],
            last_offset=_offset(last),
            wait_cut=wait_cut,
        )
        job = ResolveStreamRead(seq=seq, page=stream_page)
        return WorkflowActivationJob(resolve_stream_read=job)

    def start(how: str, seconds: int | None = 5) -> WorkflowActivationJob:
        arguments = [converter.to_payload(how), converter.to_payload(seconds)]
        return WorkflowActivationJob(
            initialize_workflow=InitializeWorkflow(arguments=arguments)
        )

    def result(completion: WorkflowActivationCompletion) -> Any:
        [complete] = completion.successful.commands
        return converter.from_payload(complete.complete_workflow_execution.result)

    config = StreamConfig([MemoryStreamStore(name="a")])
    # A single read gives a page with no Record as it is, also a cut one.
    for wait_cut in (True, False):
        with failure_note(f"wait_cut {wait_cut}"):
            instance = _new_instance(_ReadToTwoWorkflow, config)
            completion = await _activate(instance, start("read_page"))
            assert list(completion.successful.commands) == [read(1, 0)]
            completion = await _activate(instance, page(1, [], 0, wait_cut=wait_cut))
            assert result(completion) == [[], None]

    for how in ("read", "read_pages"):
        with failure_note(how):
            instance = _new_instance(_ReadToTwoWorkflow, config)
            completion = await _activate(instance, start(how))
            assert list(completion.successful.commands) == [read(1, 0)]
            completion = await _activate(instance, page(1, [1], 1))
            assert list(completion.successful.commands) == [read(2, 1)]
            # A page with no Record doesn't end the reader, whether the read limit of
            # the task cut its wait or the whole wait passed.
            completion = await _activate(instance, page(2, [], 1, wait_cut=True))
            assert list(completion.successful.commands) == [read(3, 1)]
            completion = await _activate(instance, page(3, [], 1))
            assert list(completion.successful.commands) == [read(4, 1)]
            completion = await _activate(instance, page(4, [2], 2))
            expected = [1, 2] if how == "read" else [[1], [], [], [2]]
            assert result(completion) == expected

    flag = _WorkflowLogicFlag.USE_LAST_STREAM_RECORD_OFFSET
    for replaying, flags, after in [(False, (), 1), (True, (), 0), (True, (flag,), 1)]:
        instance = _new_instance(_ReadToTwoWorkflow, config)
        await _activate(instance, start("read_pages"), replaying=replaying, flags=flags)
        completion = await _activate(
            instance, page(1, [1], 0), replaying=replaying, flags=flags
        )
        assert list(completion.successful.commands) == [read(2, after)]
        assert (flag in completion.successful.used_internal_flags) == (not replaying)

    # The first read of an iterator has the caller's wait. A later read has it too,
    # or the default of 1 minute when the caller gave none. A wait of zero stays.
    default = int(_support.DEFAULT_READ_WAIT.total_seconds())
    cases: list[tuple[int | None, int | None]] = [(None, default), (0, 0), (5, 5)]
    for how in ("read", "read_pages"):
        for given, later in cases:
            with failure_note(f"{how} with wait {given}"):
                instance = _new_instance(_ReadToTwoWorkflow, config)
                completion = await _activate(instance, start(how, given))
                assert list(completion.successful.commands) == [read(1, 0, given)]
                completion = await _activate(instance, page(1, [1], 1))
                assert list(completion.successful.commands) == [read(2, 1, later)]
                completion = await _activate(instance, page(2, [], 1))
                assert list(completion.successful.commands) == [read(3, 1, later)]
    # A single read has no default wait.
    instance = _new_instance(_ReadToTwoWorkflow, config)
    completion = await _activate(instance, start("read_page", None))
    assert list(completion.successful.commands) == [read(1, 0, None)]
