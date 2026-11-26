"""Tests of the Stream calls of one Workflow instance, with no server."""

import asyncio
import dataclasses
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

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
    ActivityCancellationType,
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
from temporalio.nexus.system.stream_service import StreamRef as ModelRef
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
    """One generated Stream call, and what Workflow code got for it."""

    operation: str
    """One of ``create``, ``append`` and ``describe``."""
    stream: str
    """The id of the Stream. A call on a Stream that an earlier phase created uses the
    Stream of that create."""
    store: str | None = None
    """The Store name that Workflow code gives, to a create or in a ref that it builds."""
    bodies: Sequence[int] = ()
    cancel: bool = False
    refusal: StreamErrorReason.ValueType | None = None
    """The reason of the failure that the test answers the call with, if any."""
    timeout: bool = False
    """Whether the Local Activity of the call ends in a timeout."""
    unserved: bool = False
    """Whether the Local Activity of the call fails since the Worker lacks the Store."""
    outcome: Any = None


class _Script:
    """The calls of :py:class:`_ScriptWorkflow`, which makes the calls of one phase
    together and then waits for all of them, and what its interceptor saw.
    """

    phases: list[list[_Call]] = []
    late_cancels = False
    """Whether a cancel comes after the task of its call ran, and not before."""
    intercepted: list[tuple[str, Any]] = []
    local_activities: list[str] = []
    """The Activity of each Local Activity that the interceptor saw."""


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
    """Logs the operation of the interceptor method of each Stream call, with its
    request, and each Local Activity.
    """

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


def _plan(
    call: _Call, seq: int, ref_store: str, store: str, history_store: str
) -> tuple[WorkflowCommand, WorkflowActivationJob, Any]:
    """Return the command that a call must send, the job that the test answers it
    with, and what Workflow code must get for it.

    ``ref_store`` is the Store name of the ref that Workflow code passes, ``store``
    the name of the Store that the SDK chose for the call, and ``history_store`` the
    one that the result of a create has.
    """
    converter = temporalio.converter.DefaultPayloadConverter()
    refused = None if call.refusal is None else core_failure(call.refusal)
    command, job = WorkflowCommand(), WorkflowActivationJob()
    outcome: Any
    if call.operation == "append":
        bodies = converter.to_payloads(call.bodies)
        stream = StreamRef(id=call.stream, store=store)
        command.append_to_stream.CopyFrom(
            AppendToStream(seq=seq, stream=stream, bodies=bodies)
        )
        outcome = (b"first%d" % seq, b"last%d" % seq)
        job.resolve_stream_append.seq = seq
        if refused:
            job.resolve_stream_append.failed.CopyFrom(refused)
        else:
            job.resolve_stream_append.appended.CopyFrom(
                StreamAppended(first_offset=outcome[0], last_offset=outcome[1])
            )
    else:
        # The request of the Local Activity has the ref of Workflow code, and the
        # Activity gets the name of the chosen Store next to it.
        ref = StreamRef(id=call.stream, store=ref_store)
        request: Message = DescribeStreamRequest(ref=ref)
        request_id = None
        described = DescribeStreamResponse(last_offset=b"last%d" % seq)
        answer: Message = described
        outcome = described.last_offset
        if call.operation == "create":
            # Each create of a run chain has the first run id as its request id.
            request, request_id = CreateStreamRequest(stream_ref=ref), "first-run"
            answer = CreateStreamResponse(
                ref=StreamRef(id=call.stream, store=history_store),
                created=seq % 2 == 0,
            )
            outcome = (history_store, seq % 2 == 0 or None)
        activity = command.schedule_local_activity
        activity.seq = seq
        activity.activity_id = str(seq)
        activity.activity_type = _STORE_CALL
        operation = request.DESCRIPTOR.name.removesuffix("Request")
        activity.arguments.extend(
            converter.to_payloads(
                [operation, store, request.SerializeToString(), request_id]
            )
        )
        activity.schedule_to_close_timeout.FromSeconds(5)
        temporalio.common.RetryPolicy(maximum_attempts=1).apply_to_proto(
            activity.retry_policy
        )
        activity.cancellation_type = ActivityCancellationType.TRY_CANCEL
        job.resolve_activity.seq = seq
        job.resolve_activity.is_local = True
        result = job.resolve_activity.result
        if call.cancel:
            result.cancelled.failure.canceled_failure_info.SetInParent()
        elif refused:
            # Core gives the failure of an attempt of a Local Activity as it is.
            result.failed.failure.CopyFrom(refused)
        elif call.unserved:
            error = ApplicationFailureInfo(type="ValueError")
            result.failed.failure.CopyFrom(
                Failure(message="No Stream Store", application_failure_info=error)
            )
        elif call.timeout:
            timeout = TimeoutFailureInfo(
                timeout_type=TimeoutType.TIMEOUT_TYPE_SCHEDULE_TO_CLOSE
            )
            result.failed.failure.CopyFrom(
                Failure(
                    message="Local Activity timed out",
                    cause=Failure(timeout_failure_info=timeout),
                    activity_failure_info=ActivityFailureInfo(
                        activity_type=ActivityType(name=_STORE_CALL),
                        retry_state=RetryState.RETRY_STATE_CANCEL_REQUESTED,
                    ),
                )
            )
        else:
            result.completed.result.CopyFrom(
                converter.to_payload(answer.SerializeToString())
            )
    if call.cancel:
        outcome = "cancelled"
    elif call.refusal is not None:
        outcome = call.refusal
    return command, job, outcome


async def test_stream_calls_get_the_results_of_their_own_seq() -> None:
    for seed in range(200):
        with failure_note(f"seed {seed}"):
            await _calls_script(random.Random(seed))


async def _calls_script(rng: random.Random) -> None:
    stores: list[StreamStore] = [MemoryStreamStore(name=name) for name in "ab"]
    selects: list[StreamSelectContext] = []

    def selector(context: StreamSelectContext) -> StreamStore:
        selects.append(context)
        if context.id == "fatal":
            raise ApplicationError("The selector has no Store for this Stream")
        return stores[int(context.id[1:]) % 2]

    config = rng.choice(
        [StreamConfig(stores[:1]), StreamConfig(stores, store_selector=selector)]
    )
    names = "a" if len(config.stores) == 1 else "ab"
    # A replay chooses the Store of each call again, as the first run did.
    replaying = rng.random() < 0.3

    # The Store name that History has for each Stream that a create of the script makes.
    # It need not be the one that the config picks for the create.
    created: dict[str, str] = {}
    phases: list[list[_Call]] = []
    streams = 0
    for _ in range(rng.randrange(1, 5)):
        phase: list[_Call] = []
        for _ in range(rng.randrange(1, 5)):
            operation = rng.choice(["create", "append", "append", "describe"])
            if operation != "create" and created and rng.random() < 0.5:
                call = _Call(operation, rng.choice(sorted(created)))
            else:
                call = _Call(operation, f"s{streams}", rng.choice([None, *names]))
                streams += 1
            if operation == "append":
                call.bodies = [rng.randrange(100) for _ in range(rng.randrange(3))]
            # The first call of a phase isn't cancelled, so each phase waits for a job.
            call.cancel = bool(phase) and rng.random() < 0.3
            call.refusal = rng.choice([None, None, None, None, *_REFUSALS])
            phase.append(call)
        phases.append(phase)
        for call in phase:
            if call.operation == "create" and not call.cancel and call.refusal is None:
                created[call.stream] = rng.choice(names)
    # The last phase can be one call that fails the Workflow Task. Its Local Activity
    # ends in an outage or finds no Store, or the selector raises for the call.
    fatal = rng.choice([None, None, "outage", "unserved", "selector"])
    if fatal == "selector" and not config.store_selector:
        fatal = None
    if fatal == "selector":
        phases.append([_Call(rng.choice(["create", "append", "describe"]), "fatal")])
    elif fatal:
        last = _Call(rng.choice(["create", "describe"]), f"s{streams}")
        last.unserved = fatal == "unserved"
        last.timeout = fatal == "outage" and rng.random() < 0.5
        if fatal == "outage" and not last.timeout:
            last.refusal = _UNAVAILABLE
        phases.append([last])

    _Script.phases, _Script.intercepted, _Script.local_activities = phases, [], []
    _Script.late_cancels = rng.random() < 0.5
    # The task also fails on a Worker that fails a Workflow for any exception.
    failure_types = rng.choice([[], [Exception]])
    instance = _new_instance(
        _ScriptWorkflow, config, [_RecordingInbound], failure_types
    )
    target = StorageDriverWorkflowInfo(
        namespace="ns", id="wf", run_id="run", type="_ScriptWorkflow"
    )
    seqs = {"append": 0, "activity": 0}
    jobs = [WorkflowActivationJob(initialize_workflow=InitializeWorkflow())]
    outcomes: list[Any] = []
    expected_selects: list[StreamSelectContext] = []
    # The created Streams that Workflow code has, which have their Store name of History
    usable: set[str] = set()
    for phase in phases:
        completion = await _activate(instance, *jobs, replaying=replaying)
        if fatal == "selector" and phase is phases[-1]:
            break
        commands: list[WorkflowCommand] = []
        cancels: list[WorkflowCommand] = []
        jobs = []
        for call in phase:
            # Each call gets its Store in Workflow code, and only a ref with no Store
            # name asks the selector.
            if call.stream in usable:
                ref_store = store = created[call.stream]
            elif call.store:
                ref_store = store = call.store
            elif config.store_selector is None:
                ref_store, store = "", "a"
            else:
                ref_store, store = "", stores[int(call.stream[1:]) % 2].name()
                expected_selects.append(
                    StreamSelectContext(id=call.stream, target=target, namespace="ns")
                )
            kind = call.operation if call.operation in seqs else "activity"
            seqs[kind] += 1
            command, job, outcome = _plan(
                call, seqs[kind], ref_store, store, created.get(call.stream, "a")
            )
            commands.append(command)
            jobs.append(job)
            outcomes.append(outcome)
            if call.cancel and kind == "activity":
                cancel = RequestCancelLocalActivity(seq=seqs[kind])
                cancels.append(WorkflowCommand(request_cancel_local_activity=cancel))
        # The commands are in the order of the calls, and a cancel sends a command only
        # for a Local Activity.
        assert list(completion.successful.commands) == commands + cancels
        usable.update(call.stream for call in phase if call.operation == "create")
        usable &= created.keys()
        # Core gives the results in any order, also those of calls that were cancelled.
        rng.shuffle(jobs)
    else:
        completion = await _activate(instance, *jobs, replaying=replaying)
    if fatal:
        assert completion.HasField("failed")
    else:
        [command] = completion.successful.commands
        assert command.HasField("complete_workflow_execution")
        calls = [call for phase in phases for call in phase]
        assert [call.outcome for call in calls] == outcomes
        assert not instance._streams._appends
        assert selects == expected_selects
        # An interceptor sees each call in the method of its operation, and the ref of
        # a create as Workflow code wrote it, also after its result.
        assert [operation for operation, _ in _Script.intercepted] == [
            call.operation for call in calls
        ]
        # The Local Activity of a create or a describe goes through the interceptors too.
        store_calls = [
            call for call in calls if call.operation in ("create", "describe")
        ]
        assert _Script.local_activities == [_STORE_CALL] * len(store_calls)
        for call, (_, request) in zip(calls, _Script.intercepted):
            if call.operation == "create":
                assert request.stream_ref.store == call.store
    # An eviction ends each task of the instance, also the task of a call that waits.
    evict = WorkflowActivationJob(remove_from_cache=RemoveFromCache())
    assert (await _activate(instance, evict)).HasField("successful")


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


async def test_the_store_of_a_call_is_chosen_before_its_command() -> None:
    operations = ["create", "append", "read", "describe"]
    converter = temporalio.converter.DefaultPayloadConverter()

    def store_of(command: WorkflowCommand) -> str:
        variant = command.WhichOneof("variant")
        if variant == "append_to_stream":
            return command.append_to_stream.stream.store
        if variant == "read_stream":
            return command.read_stream.stream.store
        # The Local Activity of a create or a describe has the Store name second.
        [_, store, *_] = command.schedule_local_activity.arguments
        return converter.from_payload(store, str)

    a, b = MemoryStreamStore(name="a"), MemoryStreamStore(name="b")
    for mask in range(2 ** len(operations)):
        # Bit `at` of `mask` gives call `at` to Store b.
        on_b = [bool(mask >> at & 1) for at in range(len(operations))]

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
                assert [store_of(command) for command in commands[:4]] == [
                    "b" if mine else "a" for mine in on_b
                ]


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
    """One generated reader, which reads pages of a Stream one after the other, and
    what Workflow code got for each page.
    """

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
    """The readers of :py:class:`_ReadsWorkflow`, and for each ``step`` Signal the
    readers that its handler cancels and the ones that it starts.
    """

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


def _body(stream: str, number: int) -> Any:
    """Return the body of a Record, which for some isn't the ``int`` of a reader."""
    return "text" if stream == "s1" and number % 3 == 0 else number


@dataclass
class _OpenRead:
    """A read whose job hasn't come, and the reader that started it."""

    stream: str
    after: int
    call: int
    cancelled: bool = False
    """Whether its reader was cancelled. SDK Core still gives the read its one job."""


async def test_a_read_page_call_is_one_command_and_one_job() -> None:
    single_batch = _WorkflowLogicFlag.PROCESS_WORKFLOW_ACTIVATION_JOBS_AS_SINGLE_BATCH
    for seed in range(300):
        # Signals run before the Stream jobs of their activation, or after all of them
        # with the single-batch flag of the History.
        for flags in ((), (single_batch,)):
            # A replay sends the same commands as the first run.
            replaying = seed % 2 == 1
            with failure_note(f"seed {seed}, flags {flags}, replaying {replaying}"):
                await _reads_script(random.Random(seed), flags, replaying)


async def _reads_script(
    rng: random.Random, flags: Sequence[int], replaying: bool
) -> None:
    converter = temporalio.converter.DefaultPayloadConverter()
    calls: list[_ReadCall] = []
    _ReadScript.calls, _ReadScript.steps = calls, []
    instance = _new_instance(
        _ReadsWorkflow, StreamConfig([MemoryStreamStore(name="a")])
    )
    initialize = WorkflowActivationJob(initialize_workflow=InitializeWorkflow())
    completion = await _activate(instance, initialize, flags=flags, replaying=replaying)
    assert not completion.successful.commands

    reads: dict[int, _OpenRead] = {}
    # The offset that each reader is at
    positions: list[int] = []
    outcomes: list[list[Any]] = []
    seqs = 0

    def call_read(call: int, commands: list[WorkflowCommand]) -> None:
        """Start the read of a reader's next page."""
        nonlocal seqs
        reader, after = calls[call], positions[call]
        if reader.body_type is None:
            # A read with no body type raises in the call, before any command.
            outcomes[call].append("TypeError")
            return
        seqs += 1
        reads[seqs] = _OpenRead(reader.stream, after, call)
        # The command has the wait and the limits of the call, and none that the call lacks.
        wait = None
        if reader.wait is not None:
            wait = Duration()
            wait.FromTimedelta(reader.wait)
        read = ReadStream(
            seq=seqs,
            stream=StreamRef(id=reader.stream, store="a"),
            from_offset=_offset(after),
            wait=wait,
            max_records=reader.max_records or 0,
            max_bytes=reader.max_bytes or 0,
        )
        commands.append(WorkflowCommand(read_stream=read))

    for step in range(rng.randrange(2, 7)):
        # Each read gets one job, also one whose reader was cancelled.
        jobs: list[ResolveStreamRead] = []
        for seq, read in reads.items():
            if rng.random() >= 0.6:
                continue
            records = [read.after + 1 + at for at in range(rng.randrange(4))]
            # The last_offset of a page can be past its last Record, also in a page with
            # no Record.
            last = (records[-1] if records else read.after) + rng.choice([0, 0, 1])
            page = StreamPage(
                records=[
                    StreamRecord(
                        offset=_offset(number),
                        body=converter.to_payload(_body(read.stream, number)),
                    )
                    for number in records
                ],
                last_offset=_offset(last),
            )
            job = ResolveStreamRead(seq=seq, page=page)
            if rng.random() < 0.15:
                not_found = StreamErrorReason.STREAM_ERROR_REASON_NOT_FOUND
                job = ResolveStreamRead(seq=seq, failed=core_failure(not_found))
            jobs.append(job)
        rng.shuffle(jobs)
        waiting = sorted(read.call for read in reads.values() if not read.cancelled)
        cancels = [call for call in waiting if rng.random() < 0.25]
        starts: list[int] = []
        for _ in range(rng.randrange(3)):
            reader = _ReadCall(
                stream=rng.choice(["s0", "s1"]),
                start=rng.choice([0, 1, 2]),
                wait=rng.choice([None, timedelta(0), timedelta(seconds=5)]),
                max_records=rng.choice([None, 2]),
                max_bytes=rng.choice([None, 100]),
                pages=rng.randrange(1, 4),
                body_type=rng.choice([int, int, int, Any, Any, None]),
            )
            starts.append(len(calls))
            calls.append(reader)
            outcomes.append([])
            positions.append(reader.start)
        _ReadScript.steps.append((cancels, starts))

        commands: list[WorkflowCommand] = []
        # The readers that got a job in this activation, in the order of the jobs
        got: list[tuple[int, ResolveStreamRead]] = []
        # Whether a reader that waits gets a body that isn't of its type. The job converts
        # the page, so this fails the Workflow Task, also if the reader is cancelled later.
        refused = False

        def apply_jobs() -> None:
            nonlocal refused
            for job in jobs:
                read = reads.pop(job.seq)
                # A cancelled reader no longer waits, so its job is dropped, whatever it is.
                if read.cancelled:
                    continue
                got.append((read.call, job))
                bodies = [converter.from_payload(r.body) for r in job.page.records]
                typed = calls[read.call].body_type is int
                refused = refused or (typed and "text" in bodies)

        def run_handler() -> None:
            for call in starts:
                call_read(call, commands)
            for call in cancels:
                outcomes[call].append("cancelled")
                # A cancel wins over a job that came for the reader in this activation.
                got[:] = [(of, job) for of, job in got if of != call]
                # No command cancels a read.
                for read in reads.values():
                    if read.call == call:
                        read.cancelled = True

        for part in (apply_jobs, run_handler) if flags else (run_handler, apply_jobs):
            part()
        for call, job in got:
            reader = calls[call]
            if job.HasField("failed"):
                outcomes[call].append(StreamErrorReason.STREAM_ERROR_REASON_NOT_FOUND)
                continue
            bodies = [converter.from_payload(r.body) for r in job.page.records]
            # The next page comes after the last_offset of this one.
            positions[call] = int.from_bytes(job.page.last_offset, "big")
            outcomes[call].append((bodies, job.page.last_offset))
            if len(outcomes[call]) < reader.pages:
                call_read(call, commands)

        signal = SignalWorkflow(signal_name="step", originating_event_id=step + 1)
        activation = [WorkflowActivationJob(signal_workflow=signal)]
        if refused and flags:
            # The jobs come first here, so the handler of the Signal would never start.
            activation = []
        activation += [WorkflowActivationJob(resolve_stream_read=job) for job in jobs]
        completion = await _activate(
            instance, *activation, flags=flags, replaying=replaying
        )
        if refused:
            assert completion.HasField("failed")
            break
        assert list(completion.successful.commands) == commands
        assert [call.outcome for call in calls] == outcomes
        # Each read leaves with its job, also a cancelled one.
        assert set(instance._streams._reads) == set(reads)

    # An eviction sends no command, also for the reads whose readers it cancels.
    evict = WorkflowActivationJob(remove_from_cache=RemoveFromCache())
    assert not (await _activate(instance, evict)).successful.commands


@workflow.defn
class _ReadToTheEndWorkflow:
    @workflow.run
    async def run(self, how: str) -> list[Any]:
        stream = workflow.get_stream(ModelRef[int](id="s", store="a"))
        wait = timedelta(seconds=5)
        if how == "read_page":
            page = await stream.read_page(wait=wait)
            return [page.records, page.last_offset]
        if how == "read":
            return [record.body async for record in stream.read(wait=wait)]
        pages = stream.read_pages(wait=wait)
        return [[record.body for record in page.records] async for page in pages]


async def test_a_read_goes_on_after_a_page_that_the_read_limit_cut() -> None:
    converter = temporalio.converter.DefaultPayloadConverter()

    def read(seq: int, after: int) -> WorkflowCommand:
        command = ReadStream(
            seq=seq,
            stream=StreamRef(id="s", store="a"),
            from_offset=_offset(after),
            wait=Duration(seconds=5),
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

    def start(how: str) -> WorkflowActivationJob:
        arguments = [converter.to_payload(how)]
        return WorkflowActivationJob(
            initialize_workflow=InitializeWorkflow(arguments=arguments)
        )

    def result(completion: WorkflowActivationCompletion) -> Any:
        [complete] = completion.successful.commands
        return converter.from_payload(complete.complete_workflow_execution.result)

    config = StreamConfig([MemoryStreamStore(name="a")])
    # A single read gives the cut page as it is, with no Record.
    instance = _new_instance(_ReadToTheEndWorkflow, config)
    completion = await _activate(instance, start("read_page"))
    assert list(completion.successful.commands) == [read(1, 0)]
    assert result(await _activate(instance, page(1, [], 0, wait_cut=True))) == [
        [],
        None,
    ]

    for how in ("read", "read_pages"):
        with failure_note(how):
            instance = _new_instance(_ReadToTheEndWorkflow, config)
            completion = await _activate(instance, start(how))
            assert list(completion.successful.commands) == [read(1, 0)]
            completion = await _activate(instance, page(1, [1], 1))
            assert list(completion.successful.commands) == [read(2, 1)]
            # The read limit of the task ended this wait, so the reader reads again.
            completion = await _activate(instance, page(2, [], 1, wait_cut=True))
            assert list(completion.successful.commands) == [read(3, 1)]
            # A page with no Record after the whole wait ends the reader.
            completion = await _activate(instance, page(3, [], 1))
            expected = [1] if how == "read" else [[1], [], []]
            assert result(completion) == expected
