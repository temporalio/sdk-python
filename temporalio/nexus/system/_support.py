from __future__ import annotations

import collections.abc
import dataclasses
import enum
import functools
import inspect
import typing
import urllib.parse
import uuid
from datetime import datetime, timedelta

import google.protobuf.duration_pb2
import google.protobuf.timestamp_pb2

import temporalio.activity
import temporalio.api.common.v1.message_pb2 as common_pb2
import temporalio.api.enums.v1.workflow_pb2 as workflow_enums_pb2
import temporalio.api.failure.v1.message_pb2 as failure_pb2
import temporalio.api.taskqueue.v1.message_pb2 as taskqueue_pb2
import temporalio.api.workflow.v1
import temporalio.common
import temporalio.converter
import temporalio.nexus.system

if typing.TYPE_CHECKING:
    import temporalio.nexus.system.stream_service.client
    import temporalio.nexus.system.stream_service.workflow._resources.stream
    from temporalio.nexus.system.stream_service.models import (
        Producer,
        ReadRecordsRequest,
        ReadRecordsResponse,
        StreamRead,
        StreamRecord,
        StreamReduce,
    )

_BodyT = typing.TypeVar("_BodyT")
_ValueT = typing.TypeVar("_ValueT")


class SignalWithStartWorkflowModelRequest(typing.Protocol):
    namespace: str
    id: str


def request_id() -> str:
    import temporalio.workflow

    # Each run of a chain sends the same create, and the Store answers a create with the
    # request id that made the Stream as a success. One id for the chain also replays.
    if temporalio.workflow.in_workflow():
        return temporalio.workflow.info().first_execution_run_id
    return str(uuid.uuid4())


def _producer_id(*parts: str) -> str:
    # Quoting keeps a ':' inside an id from shifting the parts.
    return ":".join(urllib.parse.quote(part, safe="") for part in parts)


# One id for the process, because nothing fences a client write.
@functools.cache
def _client_producer_id() -> str:
    return _producer_id("client", str(uuid.uuid4()))


def _timestamp(value: datetime) -> google.protobuf.timestamp_pb2.Timestamp:
    proto = google.protobuf.timestamp_pb2.Timestamp()
    proto.FromDatetime(value)
    return proto


def default_producer() -> "Producer":
    from temporalio.nexus.system.stream_service.models import (
        ActivityEpoch,
        ActivityProducer,
        ClientProducer,
        LocalActivityEpoch,
        LocalActivityProducer,
        Producer,
        ProducerKindClient,
        ProducerKindLocalActivity,
        ProducerKindStandaloneActivity,
        ProducerKindWorkflowActivity,
        Timestamp,
        WorkflowTaskEpoch,
    )

    if not temporalio.activity.in_activity():
        return Producer(
            id=_client_producer_id(), kind=ProducerKindClient(ClientProducer())
        )
    info = temporalio.activity.info()
    if info.is_local:
        task = info.workflow_task
        if task is None or info.workflow_run_id is None:
            raise RuntimeError(
                "A Stream write from a Local Activity needs its Workflow Task, and the "
                "Worker's Core gave none"
            )
        started_time = None
        if task.started_time is not None:
            proto_time = _timestamp(task.started_time)
            started_time = Timestamp(seconds=proto_time.seconds, nanos=proto_time.nanos)
        # The activity id of a Local Activity is its sequence number in the run, so a
        # retried Workflow Task writes as the same Producer with a higher epoch.
        return Producer(
            id=_producer_id("la", info.workflow_run_id, info.activity_id),
            kind=ProducerKindLocalActivity(
                LocalActivityProducer(
                    run_id=info.workflow_run_id,
                    epoch=LocalActivityEpoch(
                        task=WorkflowTaskEpoch(
                            started_event_id=task.started_event_id,
                            attempt=task.attempt,
                            started_time=started_time,
                        ),
                        attempt=info.attempt,
                    ),
                )
            ),
        )
    attempt_time = _timestamp(info.current_attempt_scheduled_time)
    epoch = ActivityEpoch(
        attempt_scheduled_time=Timestamp(
            seconds=attempt_time.seconds, nanos=attempt_time.nanos
        ),
        attempt=info.attempt,
    )
    if info.workflow_run_id is not None:
        # The scheduled time stays the same across attempts, and it tells apart two uses
        # of one activity id in a run.
        schedule_key = str(_timestamp(info.scheduled_time).ToNanoseconds())
        return Producer(
            id=_producer_id(
                "act", info.workflow_run_id, info.activity_id, schedule_key
            ),
            kind=ProducerKindWorkflowActivity(
                ActivityProducer(run_id=info.workflow_run_id, epoch=epoch)
            ),
        )
    if info.activity_run_id is None:
        raise RuntimeError("A Standalone Activity has no activity run id")
    return Producer(
        id=_producer_id("sa", info.activity_id, info.activity_run_id),
        kind=ProducerKindStandaloneActivity(ActivityProducer(epoch=epoch)),
    )


def workflow_producer() -> "Producer":
    import temporalio.workflow
    from temporalio.nexus.system.stream_service.models import (
        Producer,
        ProducerKindWorkflow,
        WorkflowProducer,
    )

    run_id = temporalio.workflow.info().run_id
    return Producer(
        id=_producer_id("wf", run_id),
        kind=ProducerKindWorkflow(WorkflowProducer(run_id=run_id)),
    )


def next_read_request(
    request: "ReadRecordsRequest[_BodyT]", response: "ReadRecordsResponse[_BodyT]"
) -> "ReadRecordsRequest[_BodyT] | None":
    if response.done or (request.wait is None and not response.records):
        return None
    if response.last_offset is None:
        return request
    return dataclasses.replace(request, from_offset=response.last_offset)


def resume_read_request(
    request: "ReadRecordsRequest[_BodyT]", item: "StreamRecord[_BodyT]"
) -> "ReadRecordsRequest[_BodyT]":
    return dataclasses.replace(request, from_offset=item.offset)


def retry_policy_from_proto(
    proto: common_pb2.RetryPolicy,
) -> temporalio.common.RetryPolicy:
    return temporalio.common.RetryPolicy.from_proto(proto)


def retry_policy_to_proto(
    retry_policy: temporalio.common.RetryPolicy,
) -> common_pb2.RetryPolicy:
    proto = common_pb2.RetryPolicy()
    retry_policy.apply_to_proto(proto)
    return proto


def workflow_function_name(
    value: str | collections.abc.Callable[..., collections.abc.Awaitable[object]],
) -> str:
    from temporalio.workflow import _Definition  # pyright: ignore[reportPrivateUsage]

    name, _result_type = _Definition.get_name_and_result_type(value)
    return name


def signal_function_to_proto(
    value: str | collections.abc.Callable[..., typing.Any],
) -> str:
    from temporalio.workflow import (
        _SignalDefinition,  # pyright: ignore[reportPrivateUsage]
    )

    return _SignalDefinition.must_name_from_fn_or_str(value)  # pyright: ignore[reportUnknownMemberType]


def workflow_type_to_proto(
    workflow_type: str
    | collections.abc.Callable[..., collections.abc.Awaitable[object]],
) -> common_pb2.WorkflowType:
    return common_pb2.WorkflowType(name=workflow_function_name(workflow_type))


def workflow_type_from_proto(
    proto: common_pb2.WorkflowType,
) -> str:
    return proto.name


def task_queue_from_proto(
    proto: taskqueue_pb2.TaskQueue,
) -> str:
    return proto.name


def task_queue_to_proto(
    task_queue: str,
) -> taskqueue_pb2.TaskQueue:
    return taskqueue_pb2.TaskQueue(name=task_queue)


def workflow_namespace() -> str:
    from temporalio.workflow import info

    return info().namespace


def signal_with_start_workflow_serialization_context(
    request: SignalWithStartWorkflowModelRequest,
) -> temporalio.converter.WorkflowSerializationContext:
    return temporalio.converter.WorkflowSerializationContext(
        namespace=request.namespace,
        workflow_id=request.id,
    )


def payloads_to_proto(
    values: collections.abc.Sequence[typing.Any],
) -> common_pb2.Payloads:
    return (
        temporalio.nexus.system._current_user_payload_converter().to_payloads_wrapper(
            values
        )
    )


def payloads_from_proto(
    proto: common_pb2.Payloads,
) -> list[object]:
    return list(
        temporalio.nexus.system._current_user_payload_converter().from_payloads_wrapper(
            proto
        )
    )


def _clone_payload(payload: common_pb2.Payload) -> common_pb2.Payload:
    clone = common_pb2.Payload()
    clone.CopyFrom(payload)
    return clone


def _value_to_payload(value: object | common_pb2.Payload) -> common_pb2.Payload:
    if isinstance(value, common_pb2.Payload):
        return _clone_payload(value)
    payloads = (
        temporalio.nexus.system._current_user_payload_converter().to_payloads_wrapper(
            [value]
        )
    )
    return _clone_payload(payloads.payloads[0])


def _payload_to_value(
    payload: common_pb2.Payload, type_hint: typing.Any = None
) -> object:
    return temporalio.nexus.system._current_user_payload_converter().from_payloads(
        [_clone_payload(payload)], [type_hint] if type_hint is not None else None
    )[0]


def payload_from_proto(
    proto: common_pb2.Payload,
    type_hint: typing.Any = None,
) -> object:
    return _payload_to_value(proto, type_hint)


def payload_to_proto(
    payload: object,
) -> common_pb2.Payload:
    return _value_to_payload(payload)


def failure_from_proto(
    proto: failure_pb2.Failure,
) -> BaseException:
    return temporalio.nexus.system._current_user_failure_converter().from_failure(
        proto, temporalio.nexus.system._current_user_payload_converter()
    )


def failure_to_proto(
    failure: BaseException,
) -> failure_pb2.Failure:
    proto = failure_pb2.Failure()
    temporalio.nexus.system._current_user_failure_converter().to_failure(
        failure, temporalio.nexus.system._current_user_payload_converter(), proto
    )
    return proto


def memo_from_proto(
    proto: common_pb2.Memo,
) -> collections.abc.Mapping[str, object]:
    return {key: _payload_to_value(value) for key, value in proto.fields.items()}


def memo_to_proto(
    memo: collections.abc.Mapping[str, object],
) -> common_pb2.Memo:
    message = common_pb2.Memo()
    for key, value in memo.items():
        message.fields[key].CopyFrom(_value_to_payload(value))
    return message


def header_from_proto(
    proto: common_pb2.Header,
) -> collections.abc.Mapping[str, object]:
    return {key: _payload_to_value(value) for key, value in proto.fields.items()}


def header_to_proto(
    header: collections.abc.Mapping[str, object],
) -> common_pb2.Header:
    message = common_pb2.Header()
    for key, value in header.items():
        message.fields[key].CopyFrom(_value_to_payload(value))
    return message


def duration_from_proto(proto: google.protobuf.duration_pb2.Duration) -> timedelta:
    return proto.ToTimedelta()


def duration_to_proto(
    duration: timedelta,
) -> google.protobuf.duration_pb2.Duration:
    proto = google.protobuf.duration_pb2.Duration()
    proto.FromTimedelta(duration)
    return proto


def workflow_id_reuse_policy_from_proto(
    policy: workflow_enums_pb2.WorkflowIdReusePolicy.ValueType,
) -> temporalio.common.WorkflowIDReusePolicy:
    return temporalio.common.WorkflowIDReusePolicy(int(policy))


def workflow_id_reuse_policy_to_proto(
    policy: temporalio.common.WorkflowIDReusePolicy,
) -> workflow_enums_pb2.WorkflowIdReusePolicy.ValueType:
    return typing.cast(workflow_enums_pb2.WorkflowIdReusePolicy.ValueType, int(policy))


def workflow_id_conflict_policy_from_proto(
    policy: workflow_enums_pb2.WorkflowIdConflictPolicy.ValueType,
) -> temporalio.common.WorkflowIDConflictPolicy:
    return temporalio.common.WorkflowIDConflictPolicy(int(policy))


def workflow_id_conflict_policy_to_proto(
    policy: temporalio.common.WorkflowIDConflictPolicy,
) -> workflow_enums_pb2.WorkflowIdConflictPolicy.ValueType:
    return typing.cast(
        workflow_enums_pb2.WorkflowIdConflictPolicy.ValueType, int(policy)
    )


def search_attributes_to_proto(
    search_attributes: temporalio.common.TypedSearchAttributes,
) -> common_pb2.SearchAttributes:
    proto = common_pb2.SearchAttributes()
    temporalio.converter.encode_search_attributes(search_attributes, proto)
    return proto


def search_attributes_from_proto(
    proto: common_pb2.SearchAttributes,
) -> temporalio.common.TypedSearchAttributes:
    return temporalio.converter.decode_typed_search_attributes(proto)


def priority_from_proto(
    proto: common_pb2.Priority,
) -> temporalio.common.Priority:
    return temporalio.common.Priority._from_proto(proto)  # pyright: ignore[reportPrivateUsage]


def priority_to_proto(
    priority: temporalio.common.Priority,
) -> common_pb2.Priority:
    return priority._to_proto()  # pyright: ignore[reportPrivateUsage]


def versioning_override_to_proto(
    versioning_override: temporalio.common.VersioningOverride,
) -> temporalio.api.workflow.v1.VersioningOverride:
    return versioning_override._to_proto()  # pyright: ignore[reportPrivateUsage]


def versioning_override_from_proto(
    proto: temporalio.api.workflow.v1.VersioningOverride,
) -> temporalio.common.VersioningOverride:
    if proto.HasField("pinned") and proto.pinned.HasField("version"):
        version = proto.pinned.version
        return temporalio.common.PinnedVersioningOverride(
            temporalio.common.WorkerDeploymentVersion(
                deployment_name=version.deployment_name,
                build_id=version.build_id,
            )
        )
    if proto.HasField("one_time") and proto.one_time.HasField(
        "target_deployment_version"
    ):
        version = proto.one_time.target_deployment_version
        return temporalio.common.OneTimeVersioningOverride(
            temporalio.common.WorkerDeploymentVersion(
                deployment_name=version.deployment_name,
                build_id=version.build_id,
            )
        )
    if proto.pinned_version:
        return temporalio.common.PinnedVersioningOverride(
            temporalio.common.WorkerDeploymentVersion.from_canonical_string(
                proto.pinned_version
            )
        )
    if proto.auto_upgrade:
        return temporalio.common.AutoUpgradeVersioningOverride()
    raise ValueError("unknown versioning override proto shape")


# The take, filter and reduce calls of a Stream. The generated `take`, `filter` and `reduce`
# methods of the client Stream and of the Workflow Stream call the three functions below.


class _StreamCallKind(enum.Enum):
    TAKE = "take"
    FILTER = "filter"
    REDUCE = "reduce"


@dataclasses.dataclass(frozen=True)
class _StreamCall:
    """One call after the checks of the helpers. It holds the caller's functions."""

    kind: _StreamCallKind
    from_offset: bytes | None
    timeout: timedelta
    idle_timeout: timedelta
    max_records: int
    max_bytes: int
    until: collections.abc.Callable[..., object] | None = None
    predicate: collections.abc.Callable[..., object] | None = None
    function: collections.abc.Callable[..., object] | None = None
    initial: typing.Any = None
    value_type: typing.Any = None
    """The type that Workflow code decodes the value of a fold as, or ``None`` for no hint."""

    @staticmethod
    def check(
        kind: _StreamCallKind,
        *,
        timeout: timedelta,
        from_offset: bytes | None,
        idle_timeout: timedelta | None,
        max_records: int | None,
        max_bytes: int | None,
        until: collections.abc.Callable[..., object] | None = None,
        predicate: collections.abc.Callable[..., object] | None = None,
        function: collections.abc.Callable[..., object] | None = None,
        initial: typing.Any = None,
    ) -> _StreamCall:
        if not isinstance(timeout, timedelta) or timeout <= timedelta():
            raise ValueError("timeout must be a positive timedelta")
        if idle_timeout is None:
            idle_timeout = temporalio.common.DEFAULT_STREAM_IDLE_TIMEOUT
        elif idle_timeout < timedelta():
            raise ValueError("idle_timeout must not be negative")
        if max_records is None:
            max_records = temporalio.common.DEFAULT_STREAM_MAX_RECORDS
        elif max_records < 1:
            raise ValueError("max_records must be at least 1")
        if max_bytes is None:
            max_bytes = temporalio.common.DEFAULT_STREAM_MAX_BYTES
        elif max_bytes < 1:
            raise ValueError("max_bytes must be at least 1")
        for label, value, required in (
            ("until", until, False),
            ("predicate", predicate, kind is _StreamCallKind.FILTER),
            ("function", function, kind is _StreamCallKind.REDUCE),
        ):
            _check_plain_function(label, value, required=required)
        return _StreamCall(
            kind=kind,
            from_offset=from_offset,
            timeout=timeout,
            idle_timeout=idle_timeout,
            max_records=max_records,
            max_bytes=max_bytes,
            until=until,
            predicate=predicate,
            function=function,
            initial=initial,
            value_type=_fold_value_type(function, initial)
            if function is not None
            else None,
        )


def _check_plain_function(label: str, value: object, *, required: bool) -> None:
    if value is None:
        if required:
            raise TypeError(f"{label} is required")
        return
    if not callable(value):
        raise TypeError(f"{label} must be a function")
    # The scan calls the function and uses its answer at once, so it can't await one.
    if inspect.iscoroutinefunction(value) or inspect.iscoroutinefunction(
        getattr(value, "__call__", None)
    ):
        raise TypeError(
            f"{label} must be a plain function, not a coroutine function. "
            "The scan calls it for each Record and can't await it."
        )


def _fold_value_type(
    function: collections.abc.Callable[..., object], initial: typing.Any
) -> typing.Any:
    # The generated `reduce` takes no type for the value, so Workflow code decodes the value
    # of History as the return annotation of the function, else as the type of `initial`.
    try:
        hints = typing.get_type_hints(function)
    except Exception:
        hints = {}
    if "return" in hints:
        return hints["return"]
    return type(initial) if initial is not None else None


async def _run_stream_call(stream: typing.Any, call: _StreamCall) -> typing.Any:
    # The generated modules import this module, and both packages below import them. So
    # the two imports wait for the first call.
    from temporalio.workflow._context import _Runtime

    runtime = _Runtime.maybe_current()
    if runtime is not None:
        return await runtime.workflow_stream_call(stream, call)
    from temporalio.client._stream_read import _stream_call_outside_workflow

    return await _stream_call_outside_workflow(stream, call)


async def take_stream(
    stream: temporalio.nexus.system.stream_service.client.Stream[_BodyT]
    | temporalio.nexus.system.stream_service.workflow._resources.stream.Stream[_BodyT],
    /,
    *,
    timeout: timedelta,
    until: collections.abc.Callable[[StreamRecord[_BodyT]], bool] | None,
    from_offset: bytes | None,
    idle_timeout: timedelta | None,
    max_records: int | None,
    max_bytes: int | None,
) -> StreamRead[_BodyT]:
    """Give every Record of a Stream up to a stop. The ``take`` method of a Stream calls it.

    ``until(record)`` is called for each Record, and the first Record that it accepts is the
    last one given back.

    The scan starts after ``from_offset`` and reads pages with the Store's long poll. It ends
    at the first stop that holds, in the order of ``StreamReadStop``. ``Match`` is a Record
    that ``until`` accepted. ``Done`` is the last Record of a closed Stream. ``MaxRecords``
    and ``MaxBytes`` are the limits, and ``Idle`` and ``Timeout`` are the two waits.
    ``last_offset`` in the result is where the scan stopped. Give it as the ``from_offset`` of
    the next call, so a loop loses and repeats no Record.

    Client, Activity and Workflow code call it with a client ``Stream`` or a Workflow
    ``Stream``. The scan, its stops, its defaults and its count of bytes are the same in
    each. In a client or an Activity, each page is one ``read`` of the client ``Stream``, so
    the client's interceptors see it. A Workflow ``Stream`` in an Activity reads through the
    Activity's client.

    The functions of the caller must be plain functions that change no state. The scan calls
    each one for a Record and uses its answer at once. A
    :py:class:`temporalio.contrib.streams.BufferedAppender` can append in them, and the call
    writes the appender's Records before it returns. A failed write fails the call with the
    :py:class:`temporalio.exceptions.StreamError`.

    In Workflow code only, the scan runs in one Local Activity and History gets one marker.
    The marker holds the result, with the Records and their bodies, so the encoded result
    must fit in :py:data:`temporalio.common.MAX_STREAM_RESULT_BYTES`. Records that the scan
    looked at and didn't keep stay out of History. The Worker's data converter, with its
    payload codec, encodes the marker. A replay takes the result from History, so it calls
    no Store and needs no Stream config. A replay doesn't call the functions either, so an
    appender in them writes nothing. A Workflow Task that fails and runs again calls them
    again, and an appender writes again with a higher epoch. The functions run on the
    Worker's event loop, not in the Workflow's thread. They can read Workflow state through
    a reference such as ``self``, but can't call ``temporalio.workflow``. Each sees a Record
    once, so a later change of that state doesn't check an earlier Record again.

    Args:
        stream: The Stream to scan.
        timeout: The most time for the call. It must be positive. In Workflow code a call
            keeps its Workflow Task open until it returns, so a Signal, an Update or a
            cancel reaches the Workflow only then. The default ``idle_timeout`` bounds that
            on a quiet Stream, and a short ``timeout``, such as 2 seconds, bounds it while
            Records still flow. Only a call that runs longer than about 80% of the Workflow
            Task timeout makes them wait for a Workflow Task heartbeat.
        until: Ends the call after the first Record that it accepts. ``None`` scans
            until another stop.
        from_offset: Scan the Records after this offset. ``None`` is the beginning.
        idle_timeout: End the call with ``Idle`` when no new Record arrived for this long.
            It counts from the last Record that the scan got, or from the start of the
            call. ``timedelta(0)`` gives what is in the Stream now. ``None`` is
            :py:data:`temporalio.common.DEFAULT_STREAM_IDLE_TIMEOUT`, 1 second. In Workflow
            code each Workflow Task heartbeat of a long wait adds events to History.
        max_records: The most Records to give. ``None`` is
            :py:data:`temporalio.common.DEFAULT_STREAM_MAX_RECORDS`, 1,000.
        max_bytes: A cap on the bytes of the Records to give. A Record counts its encoded
            size as the Store gave it, after the payload codec decoded it. A Record that
            would pass the cap ends the call before that Record, and the next call gets it.
            The first Record of a call is always taken, also when it is larger. ``None`` is
            :py:data:`temporalio.common.DEFAULT_STREAM_MAX_BYTES`, 256 KiB.

    Returns:
        The Records, where the scan stopped, and why.

    Raises:
        TypeError: A function is a coroutine function, or the Stream is of the wrong kind.
        ValueError: A limit is out of range.
        temporalio.exceptions.StreamError: The Store failed the call, or an appender in a
            function failed its write.
        temporalio.exceptions.StreamConfigError: In client or Activity code, the client
            has no Stream Store.
        temporalio.exceptions.ApplicationError: A function returned an awaitable. In
            Workflow code also when a function raised, when the encoded result is over
            :py:data:`temporalio.common.MAX_STREAM_RESULT_BYTES` with the ``type``
            ``StreamResultTooLarge``, or when the Worker's client has no Stream config
            with the ``type`` ``StreamConfigError``. These are not retryable, so an
            uncaught one fails the Workflow.
        temporalio.workflow.NondeterminismError: In Workflow code, a replay of a call
            whose Store, Stream id or ``from_offset`` differs from the one in History.

    .. warning::
        This API is an experimental prototype and subject to change.
    """
    call = _StreamCall.check(
        _StreamCallKind.TAKE,
        timeout=timeout,
        from_offset=from_offset,
        idle_timeout=idle_timeout,
        max_records=max_records,
        max_bytes=max_bytes,
        until=until,
    )
    return await _run_stream_call(stream, call)


async def filter_stream(
    stream: temporalio.nexus.system.stream_service.client.Stream[_BodyT]
    | temporalio.nexus.system.stream_service.workflow._resources.stream.Stream[_BodyT],
    /,
    *,
    predicate: collections.abc.Callable[[StreamRecord[_BodyT]], bool],
    timeout: timedelta,
    until: collections.abc.Callable[[StreamRecord[_BodyT]], bool] | None,
    from_offset: bytes | None,
    idle_timeout: timedelta | None,
    max_records: int | None,
    max_bytes: int | None,
) -> StreamRead[_BodyT]:
    """Give the Records that a predicate keeps, up to a stop. ``Stream.filter`` calls it.

    ``predicate(record)`` decides whether a Record is given back. ``until(record)`` is
    called for every Record that the scan looks at, kept or not. ``max_records`` and
    ``max_bytes`` count the kept Records. ``max_records=1`` gives the first Record that
    ``predicate`` keeps.

    The scan starts after ``from_offset`` and reads pages with the Store's long poll. It ends
    at the first stop that holds, in the order of ``StreamReadStop``. ``Match`` is a Record
    that ``until`` accepted. ``Done`` is the last Record of a closed Stream. ``MaxRecords``
    and ``MaxBytes`` are the limits, and ``Idle`` and ``Timeout`` are the two waits.
    ``last_offset`` in the result is where the scan stopped. Give it as the ``from_offset`` of
    the next call, so a loop loses and repeats no Record.

    Client, Activity and Workflow code call it with a client ``Stream`` or a Workflow
    ``Stream``. The scan, its stops, its defaults and its count of bytes are the same in
    each. In a client or an Activity, each page is one ``read`` of the client ``Stream``, so
    the client's interceptors see it. A Workflow ``Stream`` in an Activity reads through the
    Activity's client.

    The functions of the caller must be plain functions that change no state. The scan calls
    each one for a Record and uses its answer at once. A
    :py:class:`temporalio.contrib.streams.BufferedAppender` can append in them, and the call
    writes the appender's Records before it returns. A failed write fails the call with the
    :py:class:`temporalio.exceptions.StreamError`.

    In Workflow code only, the scan runs in one Local Activity and History gets one marker.
    The marker holds the result, with the kept Records and their bodies, so the encoded
    result must fit in :py:data:`temporalio.common.MAX_STREAM_RESULT_BYTES`. Records that
    the scan looked at and didn't keep stay out of History. The Worker's data converter,
    with its payload codec, encodes the marker. A replay takes the result from History, so
    it calls no Store and needs no Stream config. A replay doesn't call the functions
    either, so an appender in them writes nothing. A Workflow Task that fails and runs again
    calls them again, and an appender writes again with a higher epoch. The functions run
    on the Worker's event loop, not in the Workflow's thread. They can read Workflow state
    through a reference such as ``self``, but can't call ``temporalio.workflow``. Each sees
    a Record once, so a later change of that state doesn't check an earlier Record again.

    Args:
        stream: The Stream to scan.
        predicate: Keeps a Record when it returns true.
        timeout: The most time for the call. It must be positive. In Workflow code a call
            keeps its Workflow Task open until it returns, so a Signal, an Update or a
            cancel reaches the Workflow only then. The default ``idle_timeout`` bounds that
            on a quiet Stream, and a short ``timeout``, such as 2 seconds, bounds it while
            Records still flow. Only a call that runs longer than about 80% of the Workflow
            Task timeout makes them wait for a Workflow Task heartbeat.
        until: Ends the call after the first Record that it accepts, kept or not.
            ``None`` scans until another stop.
        from_offset: Scan the Records after this offset. ``None`` is the beginning.
        idle_timeout: End the call with ``Idle`` when no new Record arrived for this long.
            It counts from the last Record that the scan got, or from the start of the
            call. ``timedelta(0)`` gives what is in the Stream now. ``None`` is
            :py:data:`temporalio.common.DEFAULT_STREAM_IDLE_TIMEOUT`, 1 second. In Workflow
            code each Workflow Task heartbeat of a long wait adds events to History.
        max_records: The most Records to keep. ``None`` is
            :py:data:`temporalio.common.DEFAULT_STREAM_MAX_RECORDS`, 1,000.
        max_bytes: A cap on the bytes of the Records to keep. A Record counts its encoded
            size as the Store gave it, after the payload codec decoded it. A Record that
            would pass the cap ends the call before that Record, and the next call gets it.
            The first Record of a call is always taken, also when it is larger. ``None`` is
            :py:data:`temporalio.common.DEFAULT_STREAM_MAX_BYTES`, 256 KiB.

    Returns:
        The kept Records, where the scan stopped, and why.

    Raises:
        TypeError: A function is a coroutine function, or the Stream is of the wrong kind.
        ValueError: A limit is out of range.
        temporalio.exceptions.StreamError: The Store failed the call, or an appender in a
            function failed its write.
        temporalio.exceptions.StreamConfigError: In client or Activity code, the client
            has no Stream Store.
        temporalio.exceptions.ApplicationError: A function returned an awaitable. In
            Workflow code also when a function raised, when the encoded result is over
            :py:data:`temporalio.common.MAX_STREAM_RESULT_BYTES` with the ``type``
            ``StreamResultTooLarge``, or when the Worker's client has no Stream config
            with the ``type`` ``StreamConfigError``. These are not retryable, so an
            uncaught one fails the Workflow.
        temporalio.workflow.NondeterminismError: In Workflow code, a replay of a call
            whose Store, Stream id or ``from_offset`` differs from the one in History.

    .. warning::
        This API is an experimental prototype and subject to change.
    """
    call = _StreamCall.check(
        _StreamCallKind.FILTER,
        timeout=timeout,
        from_offset=from_offset,
        idle_timeout=idle_timeout,
        max_records=max_records,
        max_bytes=max_bytes,
        until=until,
        predicate=predicate,
    )
    return await _run_stream_call(stream, call)


async def reduce_stream(
    stream: temporalio.nexus.system.stream_service.client.Stream[_BodyT]
    | temporalio.nexus.system.stream_service.workflow._resources.stream.Stream[_BodyT],
    /,
    *,
    function: collections.abc.Callable[[_ValueT, StreamRecord[_BodyT]], _ValueT],
    initial: _ValueT,
    timeout: timedelta,
    until: collections.abc.Callable[[_ValueT, StreamRecord[_BodyT]], bool] | None,
    from_offset: bytes | None,
    idle_timeout: timedelta | None,
    max_records: int | None,
    max_bytes: int | None,
) -> StreamReduce[_ValueT]:
    """Fold the Records of a Stream into one value, up to a stop. ``Stream.reduce`` calls it.

    The scan calls ``value = function(value, record)`` for each Record in order, then
    ``until(value, record)``. No Record comes back, only the value. ``function`` must not
    change ``initial`` or a value in place.

    The scan starts after ``from_offset`` and reads pages with the Store's long poll. It ends
    at the first stop that holds, in the order of ``StreamReadStop``. ``Match`` is a Record
    that ``until`` accepted. ``Done`` is the last Record of a closed Stream. ``MaxRecords``
    and ``MaxBytes`` are the limits, and ``Idle`` and ``Timeout`` are the two waits.
    ``last_offset`` in the result is where the scan stopped. Give it as the ``from_offset`` of
    the next call, so a loop loses and repeats no Record.

    Client, Activity and Workflow code call it with a client ``Stream`` or a Workflow
    ``Stream``. The scan, its stops, its defaults and its count of bytes are the same in
    each. In a client or an Activity, each page is one ``read`` of the client ``Stream``, so
    the client's interceptors see it. A Workflow ``Stream`` in an Activity reads through the
    Activity's client.

    The functions of the caller must be plain functions that change no state. The scan calls
    each one for a Record and uses its answer at once. A
    :py:class:`temporalio.contrib.streams.BufferedAppender` can append in them, and the call
    writes the appender's Records before it returns. A failed write fails the call with the
    :py:class:`temporalio.exceptions.StreamError`.

    In Workflow code only, the scan runs in one Local Activity and History gets one marker.
    The marker holds the result, with the value, so the encoded result must fit in
    :py:data:`temporalio.common.MAX_STREAM_RESULT_BYTES`. Workflow code decodes the value as
    the return annotation of ``function``, else as the type of ``initial``, and with no type
    when ``initial`` is ``None``. Records that the scan looked at stay out of History. The
    Worker's data converter, with its payload codec, encodes the marker. A replay takes the
    result from History, so it calls no Store and needs no Stream config. A replay doesn't
    call the functions either, so an appender in them writes nothing. A Workflow Task that
    fails and runs again calls them again, and an appender writes again with a higher epoch.
    The functions run on the Worker's event loop, not in the Workflow's thread. They can
    read Workflow state through a reference such as ``self``, but can't call
    ``temporalio.workflow``. Each sees a Record once, so a later change of that state
    doesn't check an earlier Record again.

    Args:
        stream: The Stream to fold.
        function: Gives the next value from the value so far and a Record.
        initial: The value before the first Record.
        timeout: The most time for the call. It must be positive. In Workflow code a call
            keeps its Workflow Task open until it returns, so a Signal, an Update or a
            cancel reaches the Workflow only then. The default ``idle_timeout`` bounds that
            on a quiet Stream, and a short ``timeout``, such as 2 seconds, bounds it while
            Records still flow. Only a call that runs longer than about 80% of the Workflow
            Task timeout makes them wait for a Workflow Task heartbeat.
        until: Called with the value after each Record and that Record. Ends the
            fold after the first Record that it accepts.
        from_offset: Scan the Records after this offset. ``None`` is the beginning.
        idle_timeout: End the call with ``Idle`` when no new Record arrived for this long.
            It counts from the last Record that the scan got, or from the start of the
            call. ``timedelta(0)`` gives what is in the Stream now. ``None`` is
            :py:data:`temporalio.common.DEFAULT_STREAM_IDLE_TIMEOUT`, 1 second. In Workflow
            code each Workflow Task heartbeat of a long wait adds events to History.
        max_records: The most Records to fold. ``None`` is
            :py:data:`temporalio.common.DEFAULT_STREAM_MAX_RECORDS`, 1,000.
        max_bytes: A cap on the bytes of the Records to fold. A Record counts its encoded
            size as the Store gave it, after the payload codec decoded it. A Record that
            would pass the cap ends the call before that Record, and the next call gets it.
            The first Record of a call is always taken, also when it is larger. ``None`` is
            :py:data:`temporalio.common.DEFAULT_STREAM_MAX_BYTES`, 256 KiB.

    Returns:
        The value, the number of Records, where the scan stopped, and why.

    Raises:
        TypeError: A function is a coroutine function, or the Stream is of the wrong kind.
        ValueError: A limit is out of range.
        temporalio.exceptions.StreamError: The Store failed the call, or an appender in a
            function failed its write.
        temporalio.exceptions.StreamConfigError: In client or Activity code, the client
            has no Stream Store.
        temporalio.exceptions.ApplicationError: A function returned an awaitable. In
            Workflow code also when a function raised, when the encoded result is over
            :py:data:`temporalio.common.MAX_STREAM_RESULT_BYTES` with the ``type``
            ``StreamResultTooLarge``, or when the Worker's client has no Stream config
            with the ``type`` ``StreamConfigError``. These are not retryable, so an
            uncaught one fails the Workflow.
        temporalio.workflow.NondeterminismError: In Workflow code, a replay of a call
            whose Store, Stream id or ``from_offset`` differs from the one in History.

    .. warning::
        This API is an experimental prototype and subject to change.
    """
    call = _StreamCall.check(
        _StreamCallKind.REDUCE,
        timeout=timeout,
        from_offset=from_offset,
        idle_timeout=idle_timeout,
        max_records=max_records,
        max_bytes=max_bytes,
        until=until,
        function=function,
        initial=initial,
    )
    return await _run_stream_call(stream, call)
