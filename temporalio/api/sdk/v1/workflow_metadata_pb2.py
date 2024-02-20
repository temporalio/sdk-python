# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: temporal/api/sdk/v1/workflow_metadata.proto
"""Generated protocol buffer code."""
from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection
from google.protobuf import symbol_database as _symbol_database

# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()


DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(
    b'\n+temporal/api/sdk/v1/workflow_metadata.proto\x12\x13temporal.api.sdk.v1"O\n\x10WorkflowMetadata\x12;\n\ndefinition\x18\x01 \x01(\x0b\x32\'.temporal.api.sdk.v1.WorkflowDefinition"\xa6\x02\n\x12WorkflowDefinition\x12\x0c\n\x04type\x18\x01 \x01(\t\x12\x13\n\x0b\x64\x65scription\x18\x02 \x01(\t\x12M\n\x11query_definitions\x18\x03 \x03(\x0b\x32\x32.temporal.api.sdk.v1.WorkflowInteractionDefinition\x12N\n\x12signal_definitions\x18\x04 \x03(\x0b\x32\x32.temporal.api.sdk.v1.WorkflowInteractionDefinition\x12N\n\x12update_definitions\x18\x05 \x03(\x0b\x32\x32.temporal.api.sdk.v1.WorkflowInteractionDefinition"B\n\x1dWorkflowInteractionDefinition\x12\x0c\n\x04name\x18\x01 \x01(\t\x12\x13\n\x0b\x64\x65scription\x18\x02 \x01(\tB\x83\x01\n\x16io.temporal.api.sdk.v1B\x15WorkflowMetadataProtoP\x01Z\x1dgo.temporal.io/api/sdk/v1;sdk\xaa\x02\x15Temporalio.Api.Sdk.V1\xea\x02\x18Temporalio::Api::Sdk::V1b\x06proto3'
)


_WORKFLOWMETADATA = DESCRIPTOR.message_types_by_name["WorkflowMetadata"]
_WORKFLOWDEFINITION = DESCRIPTOR.message_types_by_name["WorkflowDefinition"]
_WORKFLOWINTERACTIONDEFINITION = DESCRIPTOR.message_types_by_name[
    "WorkflowInteractionDefinition"
]
WorkflowMetadata = _reflection.GeneratedProtocolMessageType(
    "WorkflowMetadata",
    (_message.Message,),
    {
        "DESCRIPTOR": _WORKFLOWMETADATA,
        "__module__": "temporal.api.sdk.v1.workflow_metadata_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.sdk.v1.WorkflowMetadata)
    },
)
_sym_db.RegisterMessage(WorkflowMetadata)

WorkflowDefinition = _reflection.GeneratedProtocolMessageType(
    "WorkflowDefinition",
    (_message.Message,),
    {
        "DESCRIPTOR": _WORKFLOWDEFINITION,
        "__module__": "temporal.api.sdk.v1.workflow_metadata_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.sdk.v1.WorkflowDefinition)
    },
)
_sym_db.RegisterMessage(WorkflowDefinition)

WorkflowInteractionDefinition = _reflection.GeneratedProtocolMessageType(
    "WorkflowInteractionDefinition",
    (_message.Message,),
    {
        "DESCRIPTOR": _WORKFLOWINTERACTIONDEFINITION,
        "__module__": "temporal.api.sdk.v1.workflow_metadata_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.sdk.v1.WorkflowInteractionDefinition)
    },
)
_sym_db.RegisterMessage(WorkflowInteractionDefinition)

if _descriptor._USE_C_DESCRIPTORS == False:
    DESCRIPTOR._options = None
    DESCRIPTOR._serialized_options = b"\n\026io.temporal.api.sdk.v1B\025WorkflowMetadataProtoP\001Z\035go.temporal.io/api/sdk/v1;sdk\252\002\025Temporalio.Api.Sdk.V1\352\002\030Temporalio::Api::Sdk::V1"
    _WORKFLOWMETADATA._serialized_start = 68
    _WORKFLOWMETADATA._serialized_end = 147
    _WORKFLOWDEFINITION._serialized_start = 150
    _WORKFLOWDEFINITION._serialized_end = 444
    _WORKFLOWINTERACTIONDEFINITION._serialized_start = 446
    _WORKFLOWINTERACTIONDEFINITION._serialized_end = 512
# @@protoc_insertion_point(module_scope)
