# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: temporal/api/common/v1/message.proto
"""Generated protocol buffer code."""
from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection
from google.protobuf import symbol_database as _symbol_database

# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()


from google.protobuf import duration_pb2 as google_dot_protobuf_dot_duration__pb2

from temporalio.api.dependencies.gogoproto import (
    gogo_pb2 as dependencies_dot_gogoproto_dot_gogo__pb2,
)
from temporalio.api.enums.v1 import (
    common_pb2 as temporal_dot_api_dot_enums_dot_v1_dot_common__pb2,
)

DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(
    b'\n$temporal/api/common/v1/message.proto\x12\x16temporal.api.common.v1\x1a\x1egoogle/protobuf/duration.proto\x1a!dependencies/gogoproto/gogo.proto\x1a"temporal/api/enums/v1/common.proto"T\n\x08\x44\x61taBlob\x12:\n\rencoding_type\x18\x01 \x01(\x0e\x32#.temporal.api.enums.v1.EncodingType\x12\x0c\n\x04\x64\x61ta\x18\x02 \x01(\x0c"=\n\x08Payloads\x12\x31\n\x08payloads\x18\x01 \x03(\x0b\x32\x1f.temporal.api.common.v1.Payload"\x89\x01\n\x07Payload\x12?\n\x08metadata\x18\x01 \x03(\x0b\x32-.temporal.api.common.v1.Payload.MetadataEntry\x12\x0c\n\x04\x64\x61ta\x18\x02 \x01(\x0c\x1a/\n\rMetadataEntry\x12\x0b\n\x03key\x18\x01 \x01(\t\x12\r\n\x05value\x18\x02 \x01(\x0c:\x02\x38\x01"\xbe\x01\n\x10SearchAttributes\x12S\n\x0eindexed_fields\x18\x01 \x03(\x0b\x32;.temporal.api.common.v1.SearchAttributes.IndexedFieldsEntry\x1aU\n\x12IndexedFieldsEntry\x12\x0b\n\x03key\x18\x01 \x01(\t\x12.\n\x05value\x18\x02 \x01(\x0b\x32\x1f.temporal.api.common.v1.Payload:\x02\x38\x01"\x90\x01\n\x04Memo\x12\x38\n\x06\x66ields\x18\x01 \x03(\x0b\x32(.temporal.api.common.v1.Memo.FieldsEntry\x1aN\n\x0b\x46ieldsEntry\x12\x0b\n\x03key\x18\x01 \x01(\t\x12.\n\x05value\x18\x02 \x01(\x0b\x32\x1f.temporal.api.common.v1.Payload:\x02\x38\x01"\x94\x01\n\x06Header\x12:\n\x06\x66ields\x18\x01 \x03(\x0b\x32*.temporal.api.common.v1.Header.FieldsEntry\x1aN\n\x0b\x46ieldsEntry\x12\x0b\n\x03key\x18\x01 \x01(\t\x12.\n\x05value\x18\x02 \x01(\x0b\x32\x1f.temporal.api.common.v1.Payload:\x02\x38\x01"8\n\x11WorkflowExecution\x12\x13\n\x0bworkflow_id\x18\x01 \x01(\t\x12\x0e\n\x06run_id\x18\x02 \x01(\t"\x1c\n\x0cWorkflowType\x12\x0c\n\x04name\x18\x01 \x01(\t"\x1c\n\x0c\x41\x63tivityType\x12\x0c\n\x04name\x18\x01 \x01(\t"\xdd\x01\n\x0bRetryPolicy\x12\x39\n\x10initial_interval\x18\x01 \x01(\x0b\x32\x19.google.protobuf.DurationB\x04\x98\xdf\x1f\x01\x12\x1b\n\x13\x62\x61\x63koff_coefficient\x18\x02 \x01(\x01\x12\x39\n\x10maximum_interval\x18\x03 \x01(\x0b\x32\x19.google.protobuf.DurationB\x04\x98\xdf\x1f\x01\x12\x18\n\x10maximum_attempts\x18\x04 \x01(\x05\x12!\n\x19non_retryable_error_types\x18\x05 \x03(\tB\x85\x01\n\x19io.temporal.api.common.v1B\x0cMessageProtoP\x01Z#go.temporal.io/api/common/v1;common\xaa\x02\x16Temporal.Api.Common.V1\xea\x02\x19Temporal::Api::Common::V1b\x06proto3'
)


_DATABLOB = DESCRIPTOR.message_types_by_name["DataBlob"]
_PAYLOADS = DESCRIPTOR.message_types_by_name["Payloads"]
_PAYLOAD = DESCRIPTOR.message_types_by_name["Payload"]
_PAYLOAD_METADATAENTRY = _PAYLOAD.nested_types_by_name["MetadataEntry"]
_SEARCHATTRIBUTES = DESCRIPTOR.message_types_by_name["SearchAttributes"]
_SEARCHATTRIBUTES_INDEXEDFIELDSENTRY = _SEARCHATTRIBUTES.nested_types_by_name[
    "IndexedFieldsEntry"
]
_MEMO = DESCRIPTOR.message_types_by_name["Memo"]
_MEMO_FIELDSENTRY = _MEMO.nested_types_by_name["FieldsEntry"]
_HEADER = DESCRIPTOR.message_types_by_name["Header"]
_HEADER_FIELDSENTRY = _HEADER.nested_types_by_name["FieldsEntry"]
_WORKFLOWEXECUTION = DESCRIPTOR.message_types_by_name["WorkflowExecution"]
_WORKFLOWTYPE = DESCRIPTOR.message_types_by_name["WorkflowType"]
_ACTIVITYTYPE = DESCRIPTOR.message_types_by_name["ActivityType"]
_RETRYPOLICY = DESCRIPTOR.message_types_by_name["RetryPolicy"]
DataBlob = _reflection.GeneratedProtocolMessageType(
    "DataBlob",
    (_message.Message,),
    {
        "DESCRIPTOR": _DATABLOB,
        "__module__": "temporal.api.common.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.common.v1.DataBlob)
    },
)
_sym_db.RegisterMessage(DataBlob)

Payloads = _reflection.GeneratedProtocolMessageType(
    "Payloads",
    (_message.Message,),
    {
        "DESCRIPTOR": _PAYLOADS,
        "__module__": "temporal.api.common.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.common.v1.Payloads)
    },
)
_sym_db.RegisterMessage(Payloads)

Payload = _reflection.GeneratedProtocolMessageType(
    "Payload",
    (_message.Message,),
    {
        "MetadataEntry": _reflection.GeneratedProtocolMessageType(
            "MetadataEntry",
            (_message.Message,),
            {
                "DESCRIPTOR": _PAYLOAD_METADATAENTRY,
                "__module__": "temporal.api.common.v1.message_pb2"
                # @@protoc_insertion_point(class_scope:temporal.api.common.v1.Payload.MetadataEntry)
            },
        ),
        "DESCRIPTOR": _PAYLOAD,
        "__module__": "temporal.api.common.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.common.v1.Payload)
    },
)
_sym_db.RegisterMessage(Payload)
_sym_db.RegisterMessage(Payload.MetadataEntry)

SearchAttributes = _reflection.GeneratedProtocolMessageType(
    "SearchAttributes",
    (_message.Message,),
    {
        "IndexedFieldsEntry": _reflection.GeneratedProtocolMessageType(
            "IndexedFieldsEntry",
            (_message.Message,),
            {
                "DESCRIPTOR": _SEARCHATTRIBUTES_INDEXEDFIELDSENTRY,
                "__module__": "temporal.api.common.v1.message_pb2"
                # @@protoc_insertion_point(class_scope:temporal.api.common.v1.SearchAttributes.IndexedFieldsEntry)
            },
        ),
        "DESCRIPTOR": _SEARCHATTRIBUTES,
        "__module__": "temporal.api.common.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.common.v1.SearchAttributes)
    },
)
_sym_db.RegisterMessage(SearchAttributes)
_sym_db.RegisterMessage(SearchAttributes.IndexedFieldsEntry)

Memo = _reflection.GeneratedProtocolMessageType(
    "Memo",
    (_message.Message,),
    {
        "FieldsEntry": _reflection.GeneratedProtocolMessageType(
            "FieldsEntry",
            (_message.Message,),
            {
                "DESCRIPTOR": _MEMO_FIELDSENTRY,
                "__module__": "temporal.api.common.v1.message_pb2"
                # @@protoc_insertion_point(class_scope:temporal.api.common.v1.Memo.FieldsEntry)
            },
        ),
        "DESCRIPTOR": _MEMO,
        "__module__": "temporal.api.common.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.common.v1.Memo)
    },
)
_sym_db.RegisterMessage(Memo)
_sym_db.RegisterMessage(Memo.FieldsEntry)

Header = _reflection.GeneratedProtocolMessageType(
    "Header",
    (_message.Message,),
    {
        "FieldsEntry": _reflection.GeneratedProtocolMessageType(
            "FieldsEntry",
            (_message.Message,),
            {
                "DESCRIPTOR": _HEADER_FIELDSENTRY,
                "__module__": "temporal.api.common.v1.message_pb2"
                # @@protoc_insertion_point(class_scope:temporal.api.common.v1.Header.FieldsEntry)
            },
        ),
        "DESCRIPTOR": _HEADER,
        "__module__": "temporal.api.common.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.common.v1.Header)
    },
)
_sym_db.RegisterMessage(Header)
_sym_db.RegisterMessage(Header.FieldsEntry)

WorkflowExecution = _reflection.GeneratedProtocolMessageType(
    "WorkflowExecution",
    (_message.Message,),
    {
        "DESCRIPTOR": _WORKFLOWEXECUTION,
        "__module__": "temporal.api.common.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.common.v1.WorkflowExecution)
    },
)
_sym_db.RegisterMessage(WorkflowExecution)

WorkflowType = _reflection.GeneratedProtocolMessageType(
    "WorkflowType",
    (_message.Message,),
    {
        "DESCRIPTOR": _WORKFLOWTYPE,
        "__module__": "temporal.api.common.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.common.v1.WorkflowType)
    },
)
_sym_db.RegisterMessage(WorkflowType)

ActivityType = _reflection.GeneratedProtocolMessageType(
    "ActivityType",
    (_message.Message,),
    {
        "DESCRIPTOR": _ACTIVITYTYPE,
        "__module__": "temporal.api.common.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.common.v1.ActivityType)
    },
)
_sym_db.RegisterMessage(ActivityType)

RetryPolicy = _reflection.GeneratedProtocolMessageType(
    "RetryPolicy",
    (_message.Message,),
    {
        "DESCRIPTOR": _RETRYPOLICY,
        "__module__": "temporal.api.common.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.common.v1.RetryPolicy)
    },
)
_sym_db.RegisterMessage(RetryPolicy)

if _descriptor._USE_C_DESCRIPTORS == False:

    DESCRIPTOR._options = None
    DESCRIPTOR._serialized_options = b"\n\031io.temporal.api.common.v1B\014MessageProtoP\001Z#go.temporal.io/api/common/v1;common\252\002\026Temporal.Api.Common.V1\352\002\031Temporal::Api::Common::V1"
    _PAYLOAD_METADATAENTRY._options = None
    _PAYLOAD_METADATAENTRY._serialized_options = b"8\001"
    _SEARCHATTRIBUTES_INDEXEDFIELDSENTRY._options = None
    _SEARCHATTRIBUTES_INDEXEDFIELDSENTRY._serialized_options = b"8\001"
    _MEMO_FIELDSENTRY._options = None
    _MEMO_FIELDSENTRY._serialized_options = b"8\001"
    _HEADER_FIELDSENTRY._options = None
    _HEADER_FIELDSENTRY._serialized_options = b"8\001"
    _RETRYPOLICY.fields_by_name["initial_interval"]._options = None
    _RETRYPOLICY.fields_by_name[
        "initial_interval"
    ]._serialized_options = b"\230\337\037\001"
    _RETRYPOLICY.fields_by_name["maximum_interval"]._options = None
    _RETRYPOLICY.fields_by_name[
        "maximum_interval"
    ]._serialized_options = b"\230\337\037\001"
    _DATABLOB._serialized_start = 167
    _DATABLOB._serialized_end = 251
    _PAYLOADS._serialized_start = 253
    _PAYLOADS._serialized_end = 314
    _PAYLOAD._serialized_start = 317
    _PAYLOAD._serialized_end = 454
    _PAYLOAD_METADATAENTRY._serialized_start = 407
    _PAYLOAD_METADATAENTRY._serialized_end = 454
    _SEARCHATTRIBUTES._serialized_start = 457
    _SEARCHATTRIBUTES._serialized_end = 647
    _SEARCHATTRIBUTES_INDEXEDFIELDSENTRY._serialized_start = 562
    _SEARCHATTRIBUTES_INDEXEDFIELDSENTRY._serialized_end = 647
    _MEMO._serialized_start = 650
    _MEMO._serialized_end = 794
    _MEMO_FIELDSENTRY._serialized_start = 716
    _MEMO_FIELDSENTRY._serialized_end = 794
    _HEADER._serialized_start = 797
    _HEADER._serialized_end = 945
    _HEADER_FIELDSENTRY._serialized_start = 716
    _HEADER_FIELDSENTRY._serialized_end = 794
    _WORKFLOWEXECUTION._serialized_start = 947
    _WORKFLOWEXECUTION._serialized_end = 1003
    _WORKFLOWTYPE._serialized_start = 1005
    _WORKFLOWTYPE._serialized_end = 1033
    _ACTIVITYTYPE._serialized_start = 1035
    _ACTIVITYTYPE._serialized_end = 1063
    _RETRYPOLICY._serialized_start = 1066
    _RETRYPOLICY._serialized_end = 1287
# @@protoc_insertion_point(module_scope)
