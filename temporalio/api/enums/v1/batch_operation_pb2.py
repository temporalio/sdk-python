# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: temporal/api/enums/v1/batch_operation.proto
"""Generated protocol buffer code."""
from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import symbol_database as _symbol_database
from google.protobuf.internal import builder as _builder

# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()


DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(
    b'\n+temporal/api/enums/v1/batch_operation.proto\x12\x15temporal.api.enums.v1*\xa0\x01\n\x12\x42\x61tchOperationType\x12$\n BATCH_OPERATION_TYPE_UNSPECIFIED\x10\x00\x12"\n\x1e\x42\x41TCH_OPERATION_TYPE_TERMINATE\x10\x01\x12\x1f\n\x1b\x42\x41TCH_OPERATION_TYPE_CANCEL\x10\x02\x12\x1f\n\x1b\x42\x41TCH_OPERATION_TYPE_SIGNAL\x10\x03*\xa6\x01\n\x13\x42\x61tchOperationState\x12%\n!BATCH_OPERATION_STATE_UNSPECIFIED\x10\x00\x12!\n\x1d\x42\x41TCH_OPERATION_STATE_RUNNING\x10\x01\x12#\n\x1f\x42\x41TCH_OPERATION_STATE_COMPLETED\x10\x02\x12 \n\x1c\x42\x41TCH_OPERATION_STATE_FAILED\x10\x03\x42\x87\x01\n\x18io.temporal.api.enums.v1B\x13\x42\x61tchOperationProtoP\x01Z!go.temporal.io/api/enums/v1;enums\xaa\x02\x15Temporal.Api.Enums.V1\xea\x02\x18Temporal::Api::Enums::V1b\x06proto3'
)

_builder.BuildMessageAndEnumDescriptors(DESCRIPTOR, globals())
_builder.BuildTopDescriptorsAndMessages(
    DESCRIPTOR, "temporal.api.enums.v1.batch_operation_pb2", globals()
)
if _descriptor._USE_C_DESCRIPTORS == False:

    DESCRIPTOR._options = None
    DESCRIPTOR._serialized_options = b"\n\030io.temporal.api.enums.v1B\023BatchOperationProtoP\001Z!go.temporal.io/api/enums/v1;enums\252\002\025Temporal.Api.Enums.V1\352\002\030Temporal::Api::Enums::V1"
    _BATCHOPERATIONTYPE._serialized_start = 71
    _BATCHOPERATIONTYPE._serialized_end = 231
    _BATCHOPERATIONSTATE._serialized_start = 234
    _BATCHOPERATIONSTATE._serialized_end = 400
# @@protoc_insertion_point(module_scope)
