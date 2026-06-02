# pyright: reportAny=false, reportExplicitAny=false

import collections.abc
import typing
from datetime import timedelta

import google.protobuf.duration_pb2

import temporalio.api.common.v1.message_pb2 as common_pb2
import temporalio.api.enums.v1.workflow_pb2 as workflow_enums_pb2
import temporalio.api.taskqueue.v1.message_pb2 as taskqueue_pb2
import temporalio.api.workflow.v1
import temporalio.common
import temporalio.converter


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


def payloads_to_proto(
    values: collections.abc.Sequence[typing.Any],
) -> common_pb2.Payloads:
    from temporalio.workflow import payload_converter

    return payload_converter().to_payloads_wrapper(values)


def _clone_payload(payload: common_pb2.Payload) -> common_pb2.Payload:
    clone = common_pb2.Payload()
    clone.CopyFrom(payload)
    return clone


def _value_to_payload(value: object | common_pb2.Payload) -> common_pb2.Payload:
    if isinstance(value, common_pb2.Payload):
        return _clone_payload(value)
    from temporalio.workflow import payload_converter

    payloads = payload_converter().to_payloads_wrapper([value])
    return _clone_payload(payloads.payloads[0])


def _payload_to_value(payload: common_pb2.Payload) -> object:
    wrapper = common_pb2.Payloads()
    wrapper.payloads.add().CopyFrom(payload)
    from temporalio.workflow import payload_converter

    return typing.cast(
        object,
        payload_converter().from_payloads_wrapper(wrapper)[0],
    )


def payload_from_proto(
    proto: common_pb2.Payload,
) -> object:
    return _payload_to_value(proto)


def payload_to_proto(
    payload: object,
) -> common_pb2.Payload:
    return _value_to_payload(payload)


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
