# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: temporal/api/update/v1/message.proto
"""Generated protocol buffer code."""
from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import symbol_database as _symbol_database
from google.protobuf.internal import builder as _builder

# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()


from temporalio.api.common.v1 import (
    message_pb2 as temporal_dot_api_dot_common_dot_v1_dot_message__pb2,
)

DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(
    b'\n$temporal/api/update/v1/message.proto\x12\x16temporal.api.update.v1\x1a$temporal/api/common/v1/message.proto"~\n\x0eWorkflowUpdate\x12.\n\x06header\x18\x01 \x01(\x0b\x32\x1e.temporal.api.common.v1.Header\x12\x0c\n\x04name\x18\x02 \x01(\t\x12.\n\x04\x61rgs\x18\x03 \x01(\x0b\x32 .temporal.api.common.v1.PayloadsB\x85\x01\n\x19io.temporal.api.update.v1B\x0cMessageProtoP\x01Z#go.temporal.io/api/update/v1;update\xaa\x02\x16Temporal.Api.Update.V1\xea\x02\x19Temporal::Api::Update::V1b\x06proto3'
)

_builder.BuildMessageAndEnumDescriptors(DESCRIPTOR, globals())
_builder.BuildTopDescriptorsAndMessages(
    DESCRIPTOR, "temporal.api.update.v1.message_pb2", globals()
)
if _descriptor._USE_C_DESCRIPTORS == False:

    DESCRIPTOR._options = None
    DESCRIPTOR._serialized_options = b"\n\031io.temporal.api.update.v1B\014MessageProtoP\001Z#go.temporal.io/api/update/v1;update\252\002\026Temporal.Api.Update.V1\352\002\031Temporal::Api::Update::V1"
    _WORKFLOWUPDATE._serialized_start = 102
    _WORKFLOWUPDATE._serialized_end = 228
# @@protoc_insertion_point(module_scope)
