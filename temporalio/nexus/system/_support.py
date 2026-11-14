import collections.abc
import dataclasses
import functools
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
    from temporalio.nexus.system.stream_service.models import (
        Producer,
        ReadRecordsRequest,
        ReadRecordsResponse,
        StreamRecord,
    )

_BodyT = typing.TypeVar("_BodyT")


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
