# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: temporal/api/failure/v1/message.proto
"""Generated protocol buffer code."""

from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection
from google.protobuf import symbol_database as _symbol_database

# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()


from google.protobuf import duration_pb2 as google_dot_protobuf_dot_duration__pb2

from temporalio.api.common.v1 import (
    message_pb2 as temporal_dot_api_dot_common_dot_v1_dot_message__pb2,
)
from temporalio.api.enums.v1 import (
    workflow_pb2 as temporal_dot_api_dot_enums_dot_v1_dot_workflow__pb2,
)

DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(
    b'\n%temporal/api/failure/v1/message.proto\x12\x17temporal.api.failure.v1\x1a$temporal/api/common/v1/message.proto\x1a$temporal/api/enums/v1/workflow.proto\x1a\x1egoogle/protobuf/duration.proto"\xa5\x01\n\x16\x41pplicationFailureInfo\x12\x0c\n\x04type\x18\x01 \x01(\t\x12\x15\n\rnon_retryable\x18\x02 \x01(\x08\x12\x31\n\x07\x64\x65tails\x18\x03 \x01(\x0b\x32 .temporal.api.common.v1.Payloads\x12\x33\n\x10next_retry_delay\x18\x04 \x01(\x0b\x32\x19.google.protobuf.Duration"\x90\x01\n\x12TimeoutFailureInfo\x12\x38\n\x0ctimeout_type\x18\x01 \x01(\x0e\x32".temporal.api.enums.v1.TimeoutType\x12@\n\x16last_heartbeat_details\x18\x02 \x01(\x0b\x32 .temporal.api.common.v1.Payloads"H\n\x13\x43\x61nceledFailureInfo\x12\x31\n\x07\x64\x65tails\x18\x01 \x01(\x0b\x32 .temporal.api.common.v1.Payloads"\x17\n\x15TerminatedFailureInfo"*\n\x11ServerFailureInfo\x12\x15\n\rnon_retryable\x18\x01 \x01(\x08"\\\n\x18ResetWorkflowFailureInfo\x12@\n\x16last_heartbeat_details\x18\x01 \x01(\x0b\x32 .temporal.api.common.v1.Payloads"\xe7\x01\n\x13\x41\x63tivityFailureInfo\x12\x1a\n\x12scheduled_event_id\x18\x01 \x01(\x03\x12\x18\n\x10started_event_id\x18\x02 \x01(\x03\x12\x10\n\x08identity\x18\x03 \x01(\t\x12;\n\ractivity_type\x18\x04 \x01(\x0b\x32$.temporal.api.common.v1.ActivityType\x12\x13\n\x0b\x61\x63tivity_id\x18\x05 \x01(\t\x12\x36\n\x0bretry_state\x18\x06 \x01(\x0e\x32!.temporal.api.enums.v1.RetryState"\xa8\x02\n!ChildWorkflowExecutionFailureInfo\x12\x11\n\tnamespace\x18\x01 \x01(\t\x12\x45\n\x12workflow_execution\x18\x02 \x01(\x0b\x32).temporal.api.common.v1.WorkflowExecution\x12;\n\rworkflow_type\x18\x03 \x01(\x0b\x32$.temporal.api.common.v1.WorkflowType\x12\x1a\n\x12initiated_event_id\x18\x04 \x01(\x03\x12\x18\n\x10started_event_id\x18\x05 \x01(\x03\x12\x36\n\x0bretry_state\x18\x06 \x01(\x0e\x32!.temporal.api.enums.v1.RetryState"\x83\x01\n\x19NexusOperationFailureInfo\x12\x1a\n\x12scheduled_event_id\x18\x01 \x01(\x03\x12\x10\n\x08\x65ndpoint\x18\x02 \x01(\t\x12\x0f\n\x07service\x18\x03 \x01(\t\x12\x11\n\toperation\x18\x04 \x01(\t\x12\x14\n\x0coperation_id\x18\x05 \x01(\t"\xc8\x07\n\x07\x46\x61ilure\x12\x0f\n\x07message\x18\x01 \x01(\t\x12\x0e\n\x06source\x18\x02 \x01(\t\x12\x13\n\x0bstack_trace\x18\x03 \x01(\t\x12;\n\x12\x65ncoded_attributes\x18\x14 \x01(\x0b\x32\x1f.temporal.api.common.v1.Payload\x12/\n\x05\x63\x61use\x18\x04 \x01(\x0b\x32 .temporal.api.failure.v1.Failure\x12S\n\x18\x61pplication_failure_info\x18\x05 \x01(\x0b\x32/.temporal.api.failure.v1.ApplicationFailureInfoH\x00\x12K\n\x14timeout_failure_info\x18\x06 \x01(\x0b\x32+.temporal.api.failure.v1.TimeoutFailureInfoH\x00\x12M\n\x15\x63\x61nceled_failure_info\x18\x07 \x01(\x0b\x32,.temporal.api.failure.v1.CanceledFailureInfoH\x00\x12Q\n\x17terminated_failure_info\x18\x08 \x01(\x0b\x32..temporal.api.failure.v1.TerminatedFailureInfoH\x00\x12I\n\x13server_failure_info\x18\t \x01(\x0b\x32*.temporal.api.failure.v1.ServerFailureInfoH\x00\x12X\n\x1breset_workflow_failure_info\x18\n \x01(\x0b\x32\x31.temporal.api.failure.v1.ResetWorkflowFailureInfoH\x00\x12M\n\x15\x61\x63tivity_failure_info\x18\x0b \x01(\x0b\x32,.temporal.api.failure.v1.ActivityFailureInfoH\x00\x12k\n%child_workflow_execution_failure_info\x18\x0c \x01(\x0b\x32:.temporal.api.failure.v1.ChildWorkflowExecutionFailureInfoH\x00\x12\x64\n&nexus_operation_execution_failure_info\x18\r \x01(\x0b\x32\x32.temporal.api.failure.v1.NexusOperationFailureInfoH\x00\x42\x0e\n\x0c\x66\x61ilure_info" \n\x1eMultiOperationExecutionAbortedB\x8e\x01\n\x1aio.temporal.api.failure.v1B\x0cMessageProtoP\x01Z%go.temporal.io/api/failure/v1;failure\xaa\x02\x19Temporalio.Api.Failure.V1\xea\x02\x1cTemporalio::Api::Failure::V1b\x06proto3'
)


_APPLICATIONFAILUREINFO = DESCRIPTOR.message_types_by_name["ApplicationFailureInfo"]
_TIMEOUTFAILUREINFO = DESCRIPTOR.message_types_by_name["TimeoutFailureInfo"]
_CANCELEDFAILUREINFO = DESCRIPTOR.message_types_by_name["CanceledFailureInfo"]
_TERMINATEDFAILUREINFO = DESCRIPTOR.message_types_by_name["TerminatedFailureInfo"]
_SERVERFAILUREINFO = DESCRIPTOR.message_types_by_name["ServerFailureInfo"]
_RESETWORKFLOWFAILUREINFO = DESCRIPTOR.message_types_by_name["ResetWorkflowFailureInfo"]
_ACTIVITYFAILUREINFO = DESCRIPTOR.message_types_by_name["ActivityFailureInfo"]
_CHILDWORKFLOWEXECUTIONFAILUREINFO = DESCRIPTOR.message_types_by_name[
    "ChildWorkflowExecutionFailureInfo"
]
_NEXUSOPERATIONFAILUREINFO = DESCRIPTOR.message_types_by_name[
    "NexusOperationFailureInfo"
]
_FAILURE = DESCRIPTOR.message_types_by_name["Failure"]
_MULTIOPERATIONEXECUTIONABORTED = DESCRIPTOR.message_types_by_name[
    "MultiOperationExecutionAborted"
]
ApplicationFailureInfo = _reflection.GeneratedProtocolMessageType(
    "ApplicationFailureInfo",
    (_message.Message,),
    {
        "DESCRIPTOR": _APPLICATIONFAILUREINFO,
        "__module__": "temporal.api.failure.v1.message_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.failure.v1.ApplicationFailureInfo)
    },
)
_sym_db.RegisterMessage(ApplicationFailureInfo)

TimeoutFailureInfo = _reflection.GeneratedProtocolMessageType(
    "TimeoutFailureInfo",
    (_message.Message,),
    {
        "DESCRIPTOR": _TIMEOUTFAILUREINFO,
        "__module__": "temporal.api.failure.v1.message_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.failure.v1.TimeoutFailureInfo)
    },
)
_sym_db.RegisterMessage(TimeoutFailureInfo)

CanceledFailureInfo = _reflection.GeneratedProtocolMessageType(
    "CanceledFailureInfo",
    (_message.Message,),
    {
        "DESCRIPTOR": _CANCELEDFAILUREINFO,
        "__module__": "temporal.api.failure.v1.message_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.failure.v1.CanceledFailureInfo)
    },
)
_sym_db.RegisterMessage(CanceledFailureInfo)

TerminatedFailureInfo = _reflection.GeneratedProtocolMessageType(
    "TerminatedFailureInfo",
    (_message.Message,),
    {
        "DESCRIPTOR": _TERMINATEDFAILUREINFO,
        "__module__": "temporal.api.failure.v1.message_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.failure.v1.TerminatedFailureInfo)
    },
)
_sym_db.RegisterMessage(TerminatedFailureInfo)

ServerFailureInfo = _reflection.GeneratedProtocolMessageType(
    "ServerFailureInfo",
    (_message.Message,),
    {
        "DESCRIPTOR": _SERVERFAILUREINFO,
        "__module__": "temporal.api.failure.v1.message_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.failure.v1.ServerFailureInfo)
    },
)
_sym_db.RegisterMessage(ServerFailureInfo)

ResetWorkflowFailureInfo = _reflection.GeneratedProtocolMessageType(
    "ResetWorkflowFailureInfo",
    (_message.Message,),
    {
        "DESCRIPTOR": _RESETWORKFLOWFAILUREINFO,
        "__module__": "temporal.api.failure.v1.message_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.failure.v1.ResetWorkflowFailureInfo)
    },
)
_sym_db.RegisterMessage(ResetWorkflowFailureInfo)

ActivityFailureInfo = _reflection.GeneratedProtocolMessageType(
    "ActivityFailureInfo",
    (_message.Message,),
    {
        "DESCRIPTOR": _ACTIVITYFAILUREINFO,
        "__module__": "temporal.api.failure.v1.message_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.failure.v1.ActivityFailureInfo)
    },
)
_sym_db.RegisterMessage(ActivityFailureInfo)

ChildWorkflowExecutionFailureInfo = _reflection.GeneratedProtocolMessageType(
    "ChildWorkflowExecutionFailureInfo",
    (_message.Message,),
    {
        "DESCRIPTOR": _CHILDWORKFLOWEXECUTIONFAILUREINFO,
        "__module__": "temporal.api.failure.v1.message_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.failure.v1.ChildWorkflowExecutionFailureInfo)
    },
)
_sym_db.RegisterMessage(ChildWorkflowExecutionFailureInfo)

NexusOperationFailureInfo = _reflection.GeneratedProtocolMessageType(
    "NexusOperationFailureInfo",
    (_message.Message,),
    {
        "DESCRIPTOR": _NEXUSOPERATIONFAILUREINFO,
        "__module__": "temporal.api.failure.v1.message_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.failure.v1.NexusOperationFailureInfo)
    },
)
_sym_db.RegisterMessage(NexusOperationFailureInfo)

Failure = _reflection.GeneratedProtocolMessageType(
    "Failure",
    (_message.Message,),
    {
        "DESCRIPTOR": _FAILURE,
        "__module__": "temporal.api.failure.v1.message_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.failure.v1.Failure)
    },
)
_sym_db.RegisterMessage(Failure)

MultiOperationExecutionAborted = _reflection.GeneratedProtocolMessageType(
    "MultiOperationExecutionAborted",
    (_message.Message,),
    {
        "DESCRIPTOR": _MULTIOPERATIONEXECUTIONABORTED,
        "__module__": "temporal.api.failure.v1.message_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.failure.v1.MultiOperationExecutionAborted)
    },
)
_sym_db.RegisterMessage(MultiOperationExecutionAborted)

if _descriptor._USE_C_DESCRIPTORS == False:
    DESCRIPTOR._options = None
    DESCRIPTOR._serialized_options = b"\n\032io.temporal.api.failure.v1B\014MessageProtoP\001Z%go.temporal.io/api/failure/v1;failure\252\002\031Temporalio.Api.Failure.V1\352\002\034Temporalio::Api::Failure::V1"
    _APPLICATIONFAILUREINFO._serialized_start = 175
    _APPLICATIONFAILUREINFO._serialized_end = 340
    _TIMEOUTFAILUREINFO._serialized_start = 343
    _TIMEOUTFAILUREINFO._serialized_end = 487
    _CANCELEDFAILUREINFO._serialized_start = 489
    _CANCELEDFAILUREINFO._serialized_end = 561
    _TERMINATEDFAILUREINFO._serialized_start = 563
    _TERMINATEDFAILUREINFO._serialized_end = 586
    _SERVERFAILUREINFO._serialized_start = 588
    _SERVERFAILUREINFO._serialized_end = 630
    _RESETWORKFLOWFAILUREINFO._serialized_start = 632
    _RESETWORKFLOWFAILUREINFO._serialized_end = 724
    _ACTIVITYFAILUREINFO._serialized_start = 727
    _ACTIVITYFAILUREINFO._serialized_end = 958
    _CHILDWORKFLOWEXECUTIONFAILUREINFO._serialized_start = 961
    _CHILDWORKFLOWEXECUTIONFAILUREINFO._serialized_end = 1257
    _NEXUSOPERATIONFAILUREINFO._serialized_start = 1260
    _NEXUSOPERATIONFAILUREINFO._serialized_end = 1391
    _FAILURE._serialized_start = 1394
    _FAILURE._serialized_end = 2362
    _MULTIOPERATIONEXECUTIONABORTED._serialized_start = 2364
    _MULTIOPERATIONEXECUTIONABORTED._serialized_end = 2396
# @@protoc_insertion_point(module_scope)
