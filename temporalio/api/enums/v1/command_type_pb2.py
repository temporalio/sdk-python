# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: temporal/api/enums/v1/command_type.proto
"""Generated protocol buffer code."""
from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection
from google.protobuf import symbol_database as _symbol_database
from google.protobuf.internal import enum_type_wrapper

# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()


DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(
    b"\n(temporal/api/enums/v1/command_type.proto\x12\x15temporal.api.enums.v1*\xf0\x04\n\x0b\x43ommandType\x12\x1c\n\x18\x43OMMAND_TYPE_UNSPECIFIED\x10\x00\x12'\n#COMMAND_TYPE_SCHEDULE_ACTIVITY_TASK\x10\x01\x12-\n)COMMAND_TYPE_REQUEST_CANCEL_ACTIVITY_TASK\x10\x02\x12\x1c\n\x18\x43OMMAND_TYPE_START_TIMER\x10\x03\x12,\n(COMMAND_TYPE_COMPLETE_WORKFLOW_EXECUTION\x10\x04\x12(\n$COMMAND_TYPE_FAIL_WORKFLOW_EXECUTION\x10\x05\x12\x1d\n\x19\x43OMMAND_TYPE_CANCEL_TIMER\x10\x06\x12*\n&COMMAND_TYPE_CANCEL_WORKFLOW_EXECUTION\x10\x07\x12;\n7COMMAND_TYPE_REQUEST_CANCEL_EXTERNAL_WORKFLOW_EXECUTION\x10\x08\x12\x1e\n\x1a\x43OMMAND_TYPE_RECORD_MARKER\x10\t\x12\x33\n/COMMAND_TYPE_CONTINUE_AS_NEW_WORKFLOW_EXECUTION\x10\n\x12/\n+COMMAND_TYPE_START_CHILD_WORKFLOW_EXECUTION\x10\x0b\x12\x33\n/COMMAND_TYPE_SIGNAL_EXTERNAL_WORKFLOW_EXECUTION\x10\x0c\x12\x32\n.COMMAND_TYPE_UPSERT_WORKFLOW_SEARCH_ATTRIBUTES\x10\rB\x84\x01\n\x18io.temporal.api.enums.v1B\x10\x43ommandTypeProtoP\x01Z!go.temporal.io/api/enums/v1;enums\xaa\x02\x15Temporal.Api.Enums.V1\xea\x02\x18Temporal::Api::Enums::V1b\x06proto3"
)

_COMMANDTYPE = DESCRIPTOR.enum_types_by_name["CommandType"]
CommandType = enum_type_wrapper.EnumTypeWrapper(_COMMANDTYPE)
COMMAND_TYPE_UNSPECIFIED = 0
COMMAND_TYPE_SCHEDULE_ACTIVITY_TASK = 1
COMMAND_TYPE_REQUEST_CANCEL_ACTIVITY_TASK = 2
COMMAND_TYPE_START_TIMER = 3
COMMAND_TYPE_COMPLETE_WORKFLOW_EXECUTION = 4
COMMAND_TYPE_FAIL_WORKFLOW_EXECUTION = 5
COMMAND_TYPE_CANCEL_TIMER = 6
COMMAND_TYPE_CANCEL_WORKFLOW_EXECUTION = 7
COMMAND_TYPE_REQUEST_CANCEL_EXTERNAL_WORKFLOW_EXECUTION = 8
COMMAND_TYPE_RECORD_MARKER = 9
COMMAND_TYPE_CONTINUE_AS_NEW_WORKFLOW_EXECUTION = 10
COMMAND_TYPE_START_CHILD_WORKFLOW_EXECUTION = 11
COMMAND_TYPE_SIGNAL_EXTERNAL_WORKFLOW_EXECUTION = 12
COMMAND_TYPE_UPSERT_WORKFLOW_SEARCH_ATTRIBUTES = 13


if _descriptor._USE_C_DESCRIPTORS == False:

    DESCRIPTOR._options = None
    DESCRIPTOR._serialized_options = b"\n\030io.temporal.api.enums.v1B\020CommandTypeProtoP\001Z!go.temporal.io/api/enums/v1;enums\252\002\025Temporal.Api.Enums.V1\352\002\030Temporal::Api::Enums::V1"
    _COMMANDTYPE._serialized_start = 68
    _COMMANDTYPE._serialized_end = 692
# @@protoc_insertion_point(module_scope)
