# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: temporal/sdk/core/activity_result/activity_result.proto
"""Generated protocol buffer code."""
from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection
from google.protobuf import symbol_database as _symbol_database

# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()


DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(
    b'\n7temporal/sdk/core/activity_result/activity_result.proto\x12\x17\x63oresdk.activity_result\x1a\x1egoogle/protobuf/duration.proto\x1a\x1fgoogle/protobuf/timestamp.proto\x1a$temporal/api/common/v1/message.proto\x1a%temporal/api/failure/v1/message.proto"\x95\x02\n\x17\x41\x63tivityExecutionResult\x12\x35\n\tcompleted\x18\x01 \x01(\x0b\x32 .coresdk.activity_result.SuccessH\x00\x12\x32\n\x06\x66\x61iled\x18\x02 \x01(\x0b\x32 .coresdk.activity_result.FailureH\x00\x12:\n\tcancelled\x18\x03 \x01(\x0b\x32%.coresdk.activity_result.CancellationH\x00\x12I\n\x13will_complete_async\x18\x04 \x01(\x0b\x32*.coresdk.activity_result.WillCompleteAsyncH\x00\x42\x08\n\x06status"\xfc\x01\n\x12\x41\x63tivityResolution\x12\x35\n\tcompleted\x18\x01 \x01(\x0b\x32 .coresdk.activity_result.SuccessH\x00\x12\x32\n\x06\x66\x61iled\x18\x02 \x01(\x0b\x32 .coresdk.activity_result.FailureH\x00\x12:\n\tcancelled\x18\x03 \x01(\x0b\x32%.coresdk.activity_result.CancellationH\x00\x12\x35\n\x07\x62\x61\x63koff\x18\x04 \x01(\x0b\x32".coresdk.activity_result.DoBackoffH\x00\x42\x08\n\x06status":\n\x07Success\x12/\n\x06result\x18\x01 \x01(\x0b\x32\x1f.temporal.api.common.v1.Payload"<\n\x07\x46\x61ilure\x12\x31\n\x07\x66\x61ilure\x18\x01 \x01(\x0b\x32 .temporal.api.failure.v1.Failure"A\n\x0c\x43\x61ncellation\x12\x31\n\x07\x66\x61ilure\x18\x01 \x01(\x0b\x32 .temporal.api.failure.v1.Failure"\x13\n\x11WillCompleteAsync"\x8d\x01\n\tDoBackoff\x12\x0f\n\x07\x61ttempt\x18\x01 \x01(\r\x12\x33\n\x10\x62\x61\x63koff_duration\x18\x02 \x01(\x0b\x32\x19.google.protobuf.Duration\x12:\n\x16original_schedule_time\x18\x03 \x01(\x0b\x32\x1a.google.protobuf.TimestampB*\xea\x02\'Temporalio::Bridge::Api::ActivityResultb\x06proto3'
)


_ACTIVITYEXECUTIONRESULT = DESCRIPTOR.message_types_by_name["ActivityExecutionResult"]
_ACTIVITYRESOLUTION = DESCRIPTOR.message_types_by_name["ActivityResolution"]
_SUCCESS = DESCRIPTOR.message_types_by_name["Success"]
_FAILURE = DESCRIPTOR.message_types_by_name["Failure"]
_CANCELLATION = DESCRIPTOR.message_types_by_name["Cancellation"]
_WILLCOMPLETEASYNC = DESCRIPTOR.message_types_by_name["WillCompleteAsync"]
_DOBACKOFF = DESCRIPTOR.message_types_by_name["DoBackoff"]
ActivityExecutionResult = _reflection.GeneratedProtocolMessageType(
    "ActivityExecutionResult",
    (_message.Message,),
    {
        "DESCRIPTOR": _ACTIVITYEXECUTIONRESULT,
        "__module__": "temporal.sdk.core.activity_result.activity_result_pb2",
        # @@protoc_insertion_point(class_scope:coresdk.activity_result.ActivityExecutionResult)
    },
)
_sym_db.RegisterMessage(ActivityExecutionResult)

ActivityResolution = _reflection.GeneratedProtocolMessageType(
    "ActivityResolution",
    (_message.Message,),
    {
        "DESCRIPTOR": _ACTIVITYRESOLUTION,
        "__module__": "temporal.sdk.core.activity_result.activity_result_pb2",
        # @@protoc_insertion_point(class_scope:coresdk.activity_result.ActivityResolution)
    },
)
_sym_db.RegisterMessage(ActivityResolution)

Success = _reflection.GeneratedProtocolMessageType(
    "Success",
    (_message.Message,),
    {
        "DESCRIPTOR": _SUCCESS,
        "__module__": "temporal.sdk.core.activity_result.activity_result_pb2",
        # @@protoc_insertion_point(class_scope:coresdk.activity_result.Success)
    },
)
_sym_db.RegisterMessage(Success)

Failure = _reflection.GeneratedProtocolMessageType(
    "Failure",
    (_message.Message,),
    {
        "DESCRIPTOR": _FAILURE,
        "__module__": "temporal.sdk.core.activity_result.activity_result_pb2",
        # @@protoc_insertion_point(class_scope:coresdk.activity_result.Failure)
    },
)
_sym_db.RegisterMessage(Failure)

Cancellation = _reflection.GeneratedProtocolMessageType(
    "Cancellation",
    (_message.Message,),
    {
        "DESCRIPTOR": _CANCELLATION,
        "__module__": "temporal.sdk.core.activity_result.activity_result_pb2",
        # @@protoc_insertion_point(class_scope:coresdk.activity_result.Cancellation)
    },
)
_sym_db.RegisterMessage(Cancellation)

WillCompleteAsync = _reflection.GeneratedProtocolMessageType(
    "WillCompleteAsync",
    (_message.Message,),
    {
        "DESCRIPTOR": _WILLCOMPLETEASYNC,
        "__module__": "temporal.sdk.core.activity_result.activity_result_pb2",
        # @@protoc_insertion_point(class_scope:coresdk.activity_result.WillCompleteAsync)
    },
)
_sym_db.RegisterMessage(WillCompleteAsync)

DoBackoff = _reflection.GeneratedProtocolMessageType(
    "DoBackoff",
    (_message.Message,),
    {
        "DESCRIPTOR": _DOBACKOFF,
        "__module__": "temporal.sdk.core.activity_result.activity_result_pb2",
        # @@protoc_insertion_point(class_scope:coresdk.activity_result.DoBackoff)
    },
)
_sym_db.RegisterMessage(DoBackoff)

if _descriptor._USE_C_DESCRIPTORS is False:
    DESCRIPTOR._options = None
    DESCRIPTOR._serialized_options = b"\352\002'Temporalio::Bridge::Api::ActivityResult"
    _ACTIVITYEXECUTIONRESULT._serialized_start = 227
    _ACTIVITYEXECUTIONRESULT._serialized_end = 504
    _ACTIVITYRESOLUTION._serialized_start = 507
    _ACTIVITYRESOLUTION._serialized_end = 759
    _SUCCESS._serialized_start = 761
    _SUCCESS._serialized_end = 819
    _FAILURE._serialized_start = 821
    _FAILURE._serialized_end = 881
    _CANCELLATION._serialized_start = 883
    _CANCELLATION._serialized_end = 948
    _WILLCOMPLETEASYNC._serialized_start = 950
    _WILLCOMPLETEASYNC._serialized_end = 969
    _DOBACKOFF._serialized_start = 972
    _DOBACKOFF._serialized_end = 1113
# @@protoc_insertion_point(module_scope)
