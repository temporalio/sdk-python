# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: temporal/api/enums/v1/interaction_type.proto
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
    b"\n,temporal/api/enums/v1/interaction_type.proto\x12\x15temporal.api.enums.v1*\xa4\x01\n\x0fInteractionType\x12 \n\x1cINTERACTION_TYPE_UNSPECIFIED\x10\x00\x12#\n\x1fINTERACTION_TYPE_WORKFLOW_QUERY\x10\x01\x12$\n INTERACTION_TYPE_WORKFLOW_UPDATE\x10\x02\x12$\n INTERACTION_TYPE_WORKFLOW_SIGNAL\x10\x03\x42\x8c\x01\n\x18io.temporal.api.enums.v1B\x14InteractionTypeProtoP\x01Z!go.temporal.io/api/enums/v1;enums\xaa\x02\x17Temporalio.Api.Enums.V1\xea\x02\x1aTemporalio::Api::Enums::V1b\x06proto3"
)

_INTERACTIONTYPE = DESCRIPTOR.enum_types_by_name["InteractionType"]
InteractionType = enum_type_wrapper.EnumTypeWrapper(_INTERACTIONTYPE)
INTERACTION_TYPE_UNSPECIFIED = 0
INTERACTION_TYPE_WORKFLOW_QUERY = 1
INTERACTION_TYPE_WORKFLOW_UPDATE = 2
INTERACTION_TYPE_WORKFLOW_SIGNAL = 3


if _descriptor._USE_C_DESCRIPTORS == False:

    DESCRIPTOR._options = None
    DESCRIPTOR._serialized_options = b"\n\030io.temporal.api.enums.v1B\024InteractionTypeProtoP\001Z!go.temporal.io/api/enums/v1;enums\252\002\027Temporalio.Api.Enums.V1\352\002\032Temporalio::Api::Enums::V1"
    _INTERACTIONTYPE._serialized_start = 72
    _INTERACTIONTYPE._serialized_end = 236
# @@protoc_insertion_point(module_scope)
