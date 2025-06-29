# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: temporal/sdk/core/nexus/nexus.proto
"""Generated protocol buffer code."""

from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection
from google.protobuf import symbol_database as _symbol_database
from google.protobuf.internal import enum_type_wrapper

# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()


from temporalio.api.common.v1 import (
    message_pb2 as temporal_dot_api_dot_common_dot_v1_dot_message__pb2,
)
from temporalio.api.failure.v1 import (
    message_pb2 as temporal_dot_api_dot_failure_dot_v1_dot_message__pb2,
)
from temporalio.api.nexus.v1 import (
    message_pb2 as temporal_dot_api_dot_nexus_dot_v1_dot_message__pb2,
)
from temporalio.api.workflowservice.v1 import (
    request_response_pb2 as temporal_dot_api_dot_workflowservice_dot_v1_dot_request__response__pb2,
)
from temporalio.bridge.proto.common import (
    common_pb2 as temporal_dot_sdk_dot_core_dot_common_dot_common__pb2,
)

DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(
    b'\n#temporal/sdk/core/nexus/nexus.proto\x12\rcoresdk.nexus\x1a$temporal/api/common/v1/message.proto\x1a%temporal/api/failure/v1/message.proto\x1a#temporal/api/nexus/v1/message.proto\x1a\x36temporal/api/workflowservice/v1/request_response.proto\x1a%temporal/sdk/core/common/common.proto"\xf8\x01\n\x14NexusOperationResult\x12\x34\n\tcompleted\x18\x01 \x01(\x0b\x32\x1f.temporal.api.common.v1.PayloadH\x00\x12\x32\n\x06\x66\x61iled\x18\x02 \x01(\x0b\x32 .temporal.api.failure.v1.FailureH\x00\x12\x35\n\tcancelled\x18\x03 \x01(\x0b\x32 .temporal.api.failure.v1.FailureH\x00\x12\x35\n\ttimed_out\x18\x04 \x01(\x0b\x32 .temporal.api.failure.v1.FailureH\x00\x42\x08\n\x06status"\xb5\x01\n\x13NexusTaskCompletion\x12\x12\n\ntask_token\x18\x01 \x01(\x0c\x12\x34\n\tcompleted\x18\x02 \x01(\x0b\x32\x1f.temporal.api.nexus.v1.ResponseH\x00\x12\x34\n\x05\x65rror\x18\x03 \x01(\x0b\x32#.temporal.api.nexus.v1.HandlerErrorH\x00\x12\x14\n\nack_cancel\x18\x04 \x01(\x08H\x00\x42\x08\n\x06status"\x9a\x01\n\tNexusTask\x12K\n\x04task\x18\x01 \x01(\x0b\x32;.temporal.api.workflowservice.v1.PollNexusTaskQueueResponseH\x00\x12\x35\n\x0b\x63\x61ncel_task\x18\x02 \x01(\x0b\x32\x1e.coresdk.nexus.CancelNexusTaskH\x00\x42\t\n\x07variant"[\n\x0f\x43\x61ncelNexusTask\x12\x12\n\ntask_token\x18\x01 \x01(\x0c\x12\x34\n\x06reason\x18\x02 \x01(\x0e\x32$.coresdk.nexus.NexusTaskCancelReason*;\n\x15NexusTaskCancelReason\x12\r\n\tTIMED_OUT\x10\x00\x12\x13\n\x0fWORKER_SHUTDOWN\x10\x01*\x7f\n\x1eNexusOperationCancellationType\x12\x1f\n\x1bWAIT_CANCELLATION_COMPLETED\x10\x00\x12\x0b\n\x07\x41\x42\x41NDON\x10\x01\x12\x0e\n\nTRY_CANCEL\x10\x02\x12\x1f\n\x1bWAIT_CANCELLATION_REQUESTED\x10\x03\x42+\xea\x02(Temporalio::Internal::Bridge::Api::Nexusb\x06proto3'
)

_NEXUSTASKCANCELREASON = DESCRIPTOR.enum_types_by_name["NexusTaskCancelReason"]
NexusTaskCancelReason = enum_type_wrapper.EnumTypeWrapper(_NEXUSTASKCANCELREASON)
_NEXUSOPERATIONCANCELLATIONTYPE = DESCRIPTOR.enum_types_by_name[
    "NexusOperationCancellationType"
]
NexusOperationCancellationType = enum_type_wrapper.EnumTypeWrapper(
    _NEXUSOPERATIONCANCELLATIONTYPE
)
TIMED_OUT = 0
WORKER_SHUTDOWN = 1
WAIT_CANCELLATION_COMPLETED = 0
ABANDON = 1
TRY_CANCEL = 2
WAIT_CANCELLATION_REQUESTED = 3


_NEXUSOPERATIONRESULT = DESCRIPTOR.message_types_by_name["NexusOperationResult"]
_NEXUSTASKCOMPLETION = DESCRIPTOR.message_types_by_name["NexusTaskCompletion"]
_NEXUSTASK = DESCRIPTOR.message_types_by_name["NexusTask"]
_CANCELNEXUSTASK = DESCRIPTOR.message_types_by_name["CancelNexusTask"]
NexusOperationResult = _reflection.GeneratedProtocolMessageType(
    "NexusOperationResult",
    (_message.Message,),
    {
        "DESCRIPTOR": _NEXUSOPERATIONRESULT,
        "__module__": "temporal.sdk.core.nexus.nexus_pb2",
        # @@protoc_insertion_point(class_scope:coresdk.nexus.NexusOperationResult)
    },
)
_sym_db.RegisterMessage(NexusOperationResult)

NexusTaskCompletion = _reflection.GeneratedProtocolMessageType(
    "NexusTaskCompletion",
    (_message.Message,),
    {
        "DESCRIPTOR": _NEXUSTASKCOMPLETION,
        "__module__": "temporal.sdk.core.nexus.nexus_pb2",
        # @@protoc_insertion_point(class_scope:coresdk.nexus.NexusTaskCompletion)
    },
)
_sym_db.RegisterMessage(NexusTaskCompletion)

NexusTask = _reflection.GeneratedProtocolMessageType(
    "NexusTask",
    (_message.Message,),
    {
        "DESCRIPTOR": _NEXUSTASK,
        "__module__": "temporal.sdk.core.nexus.nexus_pb2",
        # @@protoc_insertion_point(class_scope:coresdk.nexus.NexusTask)
    },
)
_sym_db.RegisterMessage(NexusTask)

CancelNexusTask = _reflection.GeneratedProtocolMessageType(
    "CancelNexusTask",
    (_message.Message,),
    {
        "DESCRIPTOR": _CANCELNEXUSTASK,
        "__module__": "temporal.sdk.core.nexus.nexus_pb2",
        # @@protoc_insertion_point(class_scope:coresdk.nexus.CancelNexusTask)
    },
)
_sym_db.RegisterMessage(CancelNexusTask)

if _descriptor._USE_C_DESCRIPTORS == False:
    DESCRIPTOR._options = None
    DESCRIPTOR._serialized_options = (
        b"\352\002(Temporalio::Internal::Bridge::Api::Nexus"
    )
    _NEXUSTASKCANCELREASON._serialized_start = 948
    _NEXUSTASKCANCELREASON._serialized_end = 1007
    _NEXUSOPERATIONCANCELLATIONTYPE._serialized_start = 1009
    _NEXUSOPERATIONCANCELLATIONTYPE._serialized_end = 1136
    _NEXUSOPERATIONRESULT._serialized_start = 264
    _NEXUSOPERATIONRESULT._serialized_end = 512
    _NEXUSTASKCOMPLETION._serialized_start = 515
    _NEXUSTASKCOMPLETION._serialized_end = 696
    _NEXUSTASK._serialized_start = 699
    _NEXUSTASK._serialized_end = 853
    _CANCELNEXUSTASK._serialized_start = 855
    _CANCELNEXUSTASK._serialized_end = 946
# @@protoc_insertion_point(module_scope)
