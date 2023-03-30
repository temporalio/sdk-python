# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: temporal/api/update/v1/message.proto
"""Generated protocol buffer code."""
from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection
from google.protobuf import symbol_database as _symbol_database

# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()


from temporalio.api.common.v1 import (
    message_pb2 as temporal_dot_api_dot_common_dot_v1_dot_message__pb2,
)
from temporalio.api.enums.v1 import (
    update_pb2 as temporal_dot_api_dot_enums_dot_v1_dot_update__pb2,
)
from temporalio.api.failure.v1 import (
    message_pb2 as temporal_dot_api_dot_failure_dot_v1_dot_message__pb2,
)

DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(
    b'\n$temporal/api/update/v1/message.proto\x12\x16temporal.api.update.v1\x1a$temporal/api/common/v1/message.proto\x1a"temporal/api/enums/v1/update.proto\x1a%temporal/api/failure/v1/message.proto"c\n\nWaitPolicy\x12U\n\x0flifecycle_stage\x18\x01 \x01(\x0e\x32<.temporal.api.enums.v1.UpdateWorkflowExecutionLifecycleStage"e\n\tUpdateRef\x12\x45\n\x12workflow_execution\x18\x01 \x01(\x0b\x32).temporal.api.common.v1.WorkflowExecution\x12\x11\n\tupdate_id\x18\x02 \x01(\t"|\n\x07Outcome\x12\x33\n\x07success\x18\x01 \x01(\x0b\x32 .temporal.api.common.v1.PayloadsH\x00\x12\x33\n\x07\x66\x61ilure\x18\x02 \x01(\x0b\x32 .temporal.api.failure.v1.FailureH\x00\x42\x07\n\x05value"+\n\x04Meta\x12\x11\n\tupdate_id\x18\x01 \x01(\t\x12\x10\n\x08identity\x18\x02 \x01(\t"u\n\x05Input\x12.\n\x06header\x18\x01 \x01(\x0b\x32\x1e.temporal.api.common.v1.Header\x12\x0c\n\x04name\x18\x02 \x01(\t\x12.\n\x04\x61rgs\x18\x03 \x01(\x0b\x32 .temporal.api.common.v1.Payloads"c\n\x07Request\x12*\n\x04meta\x18\x01 \x01(\x0b\x32\x1c.temporal.api.update.v1.Meta\x12,\n\x05input\x18\x02 \x01(\x0b\x32\x1d.temporal.api.update.v1.Input"\xcc\x01\n\tRejection\x12#\n\x1brejected_request_message_id\x18\x01 \x01(\t\x12,\n$rejected_request_sequencing_event_id\x18\x02 \x01(\x03\x12\x39\n\x10rejected_request\x18\x03 \x01(\x0b\x32\x1f.temporal.api.update.v1.Request\x12\x31\n\x07\x66\x61ilure\x18\x04 \x01(\x0b\x32 .temporal.api.failure.v1.Failure"\x9a\x01\n\nAcceptance\x12#\n\x1b\x61\x63\x63\x65pted_request_message_id\x18\x01 \x01(\t\x12,\n$accepted_request_sequencing_event_id\x18\x02 \x01(\x03\x12\x39\n\x10\x61\x63\x63\x65pted_request\x18\x03 \x01(\x0b\x32\x1f.temporal.api.update.v1.Request"h\n\x08Response\x12*\n\x04meta\x18\x01 \x01(\x0b\x32\x1c.temporal.api.update.v1.Meta\x12\x30\n\x07outcome\x18\x02 \x01(\x0b\x32\x1f.temporal.api.update.v1.OutcomeB\x89\x01\n\x19io.temporal.api.update.v1B\x0cMessageProtoP\x01Z#go.temporal.io/api/update/v1;update\xaa\x02\x18Temporalio.Api.Update.V1\xea\x02\x1bTemporalio::Api::Update::V1b\x06proto3'
)


_WAITPOLICY = DESCRIPTOR.message_types_by_name["WaitPolicy"]
_UPDATEREF = DESCRIPTOR.message_types_by_name["UpdateRef"]
_OUTCOME = DESCRIPTOR.message_types_by_name["Outcome"]
_META = DESCRIPTOR.message_types_by_name["Meta"]
_INPUT = DESCRIPTOR.message_types_by_name["Input"]
_REQUEST = DESCRIPTOR.message_types_by_name["Request"]
_REJECTION = DESCRIPTOR.message_types_by_name["Rejection"]
_ACCEPTANCE = DESCRIPTOR.message_types_by_name["Acceptance"]
_RESPONSE = DESCRIPTOR.message_types_by_name["Response"]
WaitPolicy = _reflection.GeneratedProtocolMessageType(
    "WaitPolicy",
    (_message.Message,),
    {
        "DESCRIPTOR": _WAITPOLICY,
        "__module__": "temporal.api.update.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.update.v1.WaitPolicy)
    },
)
_sym_db.RegisterMessage(WaitPolicy)

UpdateRef = _reflection.GeneratedProtocolMessageType(
    "UpdateRef",
    (_message.Message,),
    {
        "DESCRIPTOR": _UPDATEREF,
        "__module__": "temporal.api.update.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.update.v1.UpdateRef)
    },
)
_sym_db.RegisterMessage(UpdateRef)

Outcome = _reflection.GeneratedProtocolMessageType(
    "Outcome",
    (_message.Message,),
    {
        "DESCRIPTOR": _OUTCOME,
        "__module__": "temporal.api.update.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.update.v1.Outcome)
    },
)
_sym_db.RegisterMessage(Outcome)

Meta = _reflection.GeneratedProtocolMessageType(
    "Meta",
    (_message.Message,),
    {
        "DESCRIPTOR": _META,
        "__module__": "temporal.api.update.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.update.v1.Meta)
    },
)
_sym_db.RegisterMessage(Meta)

Input = _reflection.GeneratedProtocolMessageType(
    "Input",
    (_message.Message,),
    {
        "DESCRIPTOR": _INPUT,
        "__module__": "temporal.api.update.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.update.v1.Input)
    },
)
_sym_db.RegisterMessage(Input)

Request = _reflection.GeneratedProtocolMessageType(
    "Request",
    (_message.Message,),
    {
        "DESCRIPTOR": _REQUEST,
        "__module__": "temporal.api.update.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.update.v1.Request)
    },
)
_sym_db.RegisterMessage(Request)

Rejection = _reflection.GeneratedProtocolMessageType(
    "Rejection",
    (_message.Message,),
    {
        "DESCRIPTOR": _REJECTION,
        "__module__": "temporal.api.update.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.update.v1.Rejection)
    },
)
_sym_db.RegisterMessage(Rejection)

Acceptance = _reflection.GeneratedProtocolMessageType(
    "Acceptance",
    (_message.Message,),
    {
        "DESCRIPTOR": _ACCEPTANCE,
        "__module__": "temporal.api.update.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.update.v1.Acceptance)
    },
)
_sym_db.RegisterMessage(Acceptance)

Response = _reflection.GeneratedProtocolMessageType(
    "Response",
    (_message.Message,),
    {
        "DESCRIPTOR": _RESPONSE,
        "__module__": "temporal.api.update.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.update.v1.Response)
    },
)
_sym_db.RegisterMessage(Response)

if _descriptor._USE_C_DESCRIPTORS == False:
    DESCRIPTOR._options = None
    DESCRIPTOR._serialized_options = b"\n\031io.temporal.api.update.v1B\014MessageProtoP\001Z#go.temporal.io/api/update/v1;update\252\002\030Temporalio.Api.Update.V1\352\002\033Temporalio::Api::Update::V1"
    _WAITPOLICY._serialized_start = 177
    _WAITPOLICY._serialized_end = 276
    _UPDATEREF._serialized_start = 278
    _UPDATEREF._serialized_end = 379
    _OUTCOME._serialized_start = 381
    _OUTCOME._serialized_end = 505
    _META._serialized_start = 507
    _META._serialized_end = 550
    _INPUT._serialized_start = 552
    _INPUT._serialized_end = 669
    _REQUEST._serialized_start = 671
    _REQUEST._serialized_end = 770
    _REJECTION._serialized_start = 773
    _REJECTION._serialized_end = 977
    _ACCEPTANCE._serialized_start = 980
    _ACCEPTANCE._serialized_end = 1134
    _RESPONSE._serialized_start = 1136
    _RESPONSE._serialized_end = 1240
# @@protoc_insertion_point(module_scope)
