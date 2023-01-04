# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: temporal/api/namespace/v1/message.proto
"""Generated protocol buffer code."""
from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection
from google.protobuf import symbol_database as _symbol_database

# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()


from google.protobuf import duration_pb2 as google_dot_protobuf_dot_duration__pb2
from google.protobuf import timestamp_pb2 as google_dot_protobuf_dot_timestamp__pb2

from temporalio.api.dependencies.gogoproto import (
    gogo_pb2 as dependencies_dot_gogoproto_dot_gogo__pb2,
)
from temporalio.api.enums.v1 import (
    namespace_pb2 as temporal_dot_api_dot_enums_dot_v1_dot_namespace__pb2,
)

DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(
    b'\n\'temporal/api/namespace/v1/message.proto\x12\x19temporal.api.namespace.v1\x1a\x1egoogle/protobuf/duration.proto\x1a\x1fgoogle/protobuf/timestamp.proto\x1a!dependencies/gogoproto/gogo.proto\x1a%temporal/api/enums/v1/namespace.proto"\x94\x02\n\rNamespaceInfo\x12\x0c\n\x04name\x18\x01 \x01(\t\x12\x34\n\x05state\x18\x02 \x01(\x0e\x32%.temporal.api.enums.v1.NamespaceState\x12\x13\n\x0b\x64\x65scription\x18\x03 \x01(\t\x12\x13\n\x0bowner_email\x18\x04 \x01(\t\x12@\n\x04\x64\x61ta\x18\x05 \x03(\x0b\x32\x32.temporal.api.namespace.v1.NamespaceInfo.DataEntry\x12\n\n\x02id\x18\x06 \x01(\t\x12\x1a\n\x12supports_schedules\x18\x64 \x01(\x08\x1a+\n\tDataEntry\x12\x0b\n\x03key\x18\x01 \x01(\t\x12\r\n\x05value\x18\x02 \x01(\t:\x02\x38\x01"\xe8\x02\n\x0fNamespaceConfig\x12I\n workflow_execution_retention_ttl\x18\x01 \x01(\x0b\x32\x19.google.protobuf.DurationB\x04\x98\xdf\x1f\x01\x12<\n\x0c\x62\x61\x64_binaries\x18\x02 \x01(\x0b\x32&.temporal.api.namespace.v1.BadBinaries\x12\x44\n\x16history_archival_state\x18\x03 \x01(\x0e\x32$.temporal.api.enums.v1.ArchivalState\x12\x1c\n\x14history_archival_uri\x18\x04 \x01(\t\x12G\n\x19visibility_archival_state\x18\x05 \x01(\x0e\x32$.temporal.api.enums.v1.ArchivalState\x12\x1f\n\x17visibility_archival_uri\x18\x06 \x01(\t"\xb0\x01\n\x0b\x42\x61\x64\x42inaries\x12\x46\n\x08\x62inaries\x18\x01 \x03(\x0b\x32\x34.temporal.api.namespace.v1.BadBinaries.BinariesEntry\x1aY\n\rBinariesEntry\x12\x0b\n\x03key\x18\x01 \x01(\t\x12\x37\n\x05value\x18\x02 \x01(\x0b\x32(.temporal.api.namespace.v1.BadBinaryInfo:\x02\x38\x01"h\n\rBadBinaryInfo\x12\x0e\n\x06reason\x18\x01 \x01(\t\x12\x10\n\x08operator\x18\x02 \x01(\t\x12\x35\n\x0b\x63reate_time\x18\x03 \x01(\x0b\x32\x1a.google.protobuf.TimestampB\x04\x90\xdf\x1f\x01"\xea\x01\n\x13UpdateNamespaceInfo\x12\x13\n\x0b\x64\x65scription\x18\x01 \x01(\t\x12\x13\n\x0bowner_email\x18\x02 \x01(\t\x12\x46\n\x04\x64\x61ta\x18\x03 \x03(\x0b\x32\x38.temporal.api.namespace.v1.UpdateNamespaceInfo.DataEntry\x12\x34\n\x05state\x18\x04 \x01(\x0e\x32%.temporal.api.enums.v1.NamespaceState\x1a+\n\tDataEntry\x12\x0b\n\x03key\x18\x01 \x01(\t\x12\r\n\x05value\x18\x02 \x01(\t:\x02\x38\x01"*\n\x0fNamespaceFilter\x12\x17\n\x0finclude_deleted\x18\x01 \x01(\x08\x42\x98\x01\n\x1cio.temporal.api.namespace.v1B\x0cMessageProtoP\x01Z)go.temporal.io/api/namespace/v1;namespace\xaa\x02\x1bTemporalio.Api.Namespace.V1\xea\x02\x1eTemporalio::Api::Namespace::V1b\x06proto3'
)


_NAMESPACEINFO = DESCRIPTOR.message_types_by_name["NamespaceInfo"]
_NAMESPACEINFO_DATAENTRY = _NAMESPACEINFO.nested_types_by_name["DataEntry"]
_NAMESPACECONFIG = DESCRIPTOR.message_types_by_name["NamespaceConfig"]
_BADBINARIES = DESCRIPTOR.message_types_by_name["BadBinaries"]
_BADBINARIES_BINARIESENTRY = _BADBINARIES.nested_types_by_name["BinariesEntry"]
_BADBINARYINFO = DESCRIPTOR.message_types_by_name["BadBinaryInfo"]
_UPDATENAMESPACEINFO = DESCRIPTOR.message_types_by_name["UpdateNamespaceInfo"]
_UPDATENAMESPACEINFO_DATAENTRY = _UPDATENAMESPACEINFO.nested_types_by_name["DataEntry"]
_NAMESPACEFILTER = DESCRIPTOR.message_types_by_name["NamespaceFilter"]
NamespaceInfo = _reflection.GeneratedProtocolMessageType(
    "NamespaceInfo",
    (_message.Message,),
    {
        "DataEntry": _reflection.GeneratedProtocolMessageType(
            "DataEntry",
            (_message.Message,),
            {
                "DESCRIPTOR": _NAMESPACEINFO_DATAENTRY,
                "__module__": "temporal.api.namespace.v1.message_pb2"
                # @@protoc_insertion_point(class_scope:temporal.api.namespace.v1.NamespaceInfo.DataEntry)
            },
        ),
        "DESCRIPTOR": _NAMESPACEINFO,
        "__module__": "temporal.api.namespace.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.namespace.v1.NamespaceInfo)
    },
)
_sym_db.RegisterMessage(NamespaceInfo)
_sym_db.RegisterMessage(NamespaceInfo.DataEntry)

NamespaceConfig = _reflection.GeneratedProtocolMessageType(
    "NamespaceConfig",
    (_message.Message,),
    {
        "DESCRIPTOR": _NAMESPACECONFIG,
        "__module__": "temporal.api.namespace.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.namespace.v1.NamespaceConfig)
    },
)
_sym_db.RegisterMessage(NamespaceConfig)

BadBinaries = _reflection.GeneratedProtocolMessageType(
    "BadBinaries",
    (_message.Message,),
    {
        "BinariesEntry": _reflection.GeneratedProtocolMessageType(
            "BinariesEntry",
            (_message.Message,),
            {
                "DESCRIPTOR": _BADBINARIES_BINARIESENTRY,
                "__module__": "temporal.api.namespace.v1.message_pb2"
                # @@protoc_insertion_point(class_scope:temporal.api.namespace.v1.BadBinaries.BinariesEntry)
            },
        ),
        "DESCRIPTOR": _BADBINARIES,
        "__module__": "temporal.api.namespace.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.namespace.v1.BadBinaries)
    },
)
_sym_db.RegisterMessage(BadBinaries)
_sym_db.RegisterMessage(BadBinaries.BinariesEntry)

BadBinaryInfo = _reflection.GeneratedProtocolMessageType(
    "BadBinaryInfo",
    (_message.Message,),
    {
        "DESCRIPTOR": _BADBINARYINFO,
        "__module__": "temporal.api.namespace.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.namespace.v1.BadBinaryInfo)
    },
)
_sym_db.RegisterMessage(BadBinaryInfo)

UpdateNamespaceInfo = _reflection.GeneratedProtocolMessageType(
    "UpdateNamespaceInfo",
    (_message.Message,),
    {
        "DataEntry": _reflection.GeneratedProtocolMessageType(
            "DataEntry",
            (_message.Message,),
            {
                "DESCRIPTOR": _UPDATENAMESPACEINFO_DATAENTRY,
                "__module__": "temporal.api.namespace.v1.message_pb2"
                # @@protoc_insertion_point(class_scope:temporal.api.namespace.v1.UpdateNamespaceInfo.DataEntry)
            },
        ),
        "DESCRIPTOR": _UPDATENAMESPACEINFO,
        "__module__": "temporal.api.namespace.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.namespace.v1.UpdateNamespaceInfo)
    },
)
_sym_db.RegisterMessage(UpdateNamespaceInfo)
_sym_db.RegisterMessage(UpdateNamespaceInfo.DataEntry)

NamespaceFilter = _reflection.GeneratedProtocolMessageType(
    "NamespaceFilter",
    (_message.Message,),
    {
        "DESCRIPTOR": _NAMESPACEFILTER,
        "__module__": "temporal.api.namespace.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.namespace.v1.NamespaceFilter)
    },
)
_sym_db.RegisterMessage(NamespaceFilter)

if _descriptor._USE_C_DESCRIPTORS == False:

    DESCRIPTOR._options = None
    DESCRIPTOR._serialized_options = b"\n\034io.temporal.api.namespace.v1B\014MessageProtoP\001Z)go.temporal.io/api/namespace/v1;namespace\252\002\033Temporalio.Api.Namespace.V1\352\002\036Temporalio::Api::Namespace::V1"
    _NAMESPACEINFO_DATAENTRY._options = None
    _NAMESPACEINFO_DATAENTRY._serialized_options = b"8\001"
    _NAMESPACECONFIG.fields_by_name["workflow_execution_retention_ttl"]._options = None
    _NAMESPACECONFIG.fields_by_name[
        "workflow_execution_retention_ttl"
    ]._serialized_options = b"\230\337\037\001"
    _BADBINARIES_BINARIESENTRY._options = None
    _BADBINARIES_BINARIESENTRY._serialized_options = b"8\001"
    _BADBINARYINFO.fields_by_name["create_time"]._options = None
    _BADBINARYINFO.fields_by_name[
        "create_time"
    ]._serialized_options = b"\220\337\037\001"
    _UPDATENAMESPACEINFO_DATAENTRY._options = None
    _UPDATENAMESPACEINFO_DATAENTRY._serialized_options = b"8\001"
    _NAMESPACEINFO._serialized_start = 210
    _NAMESPACEINFO._serialized_end = 486
    _NAMESPACEINFO_DATAENTRY._serialized_start = 443
    _NAMESPACEINFO_DATAENTRY._serialized_end = 486
    _NAMESPACECONFIG._serialized_start = 489
    _NAMESPACECONFIG._serialized_end = 849
    _BADBINARIES._serialized_start = 852
    _BADBINARIES._serialized_end = 1028
    _BADBINARIES_BINARIESENTRY._serialized_start = 939
    _BADBINARIES_BINARIESENTRY._serialized_end = 1028
    _BADBINARYINFO._serialized_start = 1030
    _BADBINARYINFO._serialized_end = 1134
    _UPDATENAMESPACEINFO._serialized_start = 1137
    _UPDATENAMESPACEINFO._serialized_end = 1371
    _UPDATENAMESPACEINFO_DATAENTRY._serialized_start = 443
    _UPDATENAMESPACEINFO_DATAENTRY._serialized_end = 486
    _NAMESPACEFILTER._serialized_start = 1373
    _NAMESPACEFILTER._serialized_end = 1415
# @@protoc_insertion_point(module_scope)
