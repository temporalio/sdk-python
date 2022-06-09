# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: temporal/api/version/v1/message.proto
"""Generated protocol buffer code."""
from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection
from google.protobuf import symbol_database as _symbol_database

# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()


from google.protobuf import timestamp_pb2 as google_dot_protobuf_dot_timestamp__pb2

from temporalio.api.dependencies.gogoproto import (
    gogo_pb2 as dependencies_dot_gogoproto_dot_gogo__pb2,
)
from temporalio.api.enums.v1 import (
    common_pb2 as temporal_dot_api_dot_enums_dot_v1_dot_common__pb2,
)

DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(
    b'\n%temporal/api/version/v1/message.proto\x12\x17temporal.api.version.v1\x1a\x1fgoogle/protobuf/timestamp.proto\x1a!dependencies/gogoproto/gogo.proto\x1a"temporal/api/enums/v1/common.proto"e\n\x0bReleaseInfo\x12\x0f\n\x07version\x18\x01 \x01(\t\x12\x36\n\x0crelease_time\x18\x02 \x01(\x0b\x32\x1a.google.protobuf.TimestampB\x04\x90\xdf\x1f\x01\x12\r\n\x05notes\x18\x03 \x01(\t"K\n\x05\x41lert\x12\x0f\n\x07message\x18\x01 \x01(\t\x12\x31\n\x08severity\x18\x02 \x01(\x0e\x32\x1f.temporal.api.enums.v1.Severity"\x81\x02\n\x0bVersionInfo\x12\x35\n\x07\x63urrent\x18\x01 \x01(\x0b\x32$.temporal.api.version.v1.ReleaseInfo\x12\x39\n\x0brecommended\x18\x02 \x01(\x0b\x32$.temporal.api.version.v1.ReleaseInfo\x12\x14\n\x0cinstructions\x18\x03 \x01(\t\x12.\n\x06\x61lerts\x18\x04 \x03(\x0b\x32\x1e.temporal.api.version.v1.Alert\x12:\n\x10last_update_time\x18\x05 \x01(\x0b\x32\x1a.google.protobuf.TimestampB\x04\x90\xdf\x1f\x01\x42\x8a\x01\n\x1aio.temporal.api.version.v1B\x0cMessageProtoP\x01Z%go.temporal.io/api/version/v1;version\xaa\x02\x17Temporal.Api.Version.V1\xea\x02\x1aTemporal::Api::Version::V1b\x06proto3'
)


_RELEASEINFO = DESCRIPTOR.message_types_by_name["ReleaseInfo"]
_ALERT = DESCRIPTOR.message_types_by_name["Alert"]
_VERSIONINFO = DESCRIPTOR.message_types_by_name["VersionInfo"]
ReleaseInfo = _reflection.GeneratedProtocolMessageType(
    "ReleaseInfo",
    (_message.Message,),
    {
        "DESCRIPTOR": _RELEASEINFO,
        "__module__": "temporal.api.version.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.version.v1.ReleaseInfo)
    },
)
_sym_db.RegisterMessage(ReleaseInfo)

Alert = _reflection.GeneratedProtocolMessageType(
    "Alert",
    (_message.Message,),
    {
        "DESCRIPTOR": _ALERT,
        "__module__": "temporal.api.version.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.version.v1.Alert)
    },
)
_sym_db.RegisterMessage(Alert)

VersionInfo = _reflection.GeneratedProtocolMessageType(
    "VersionInfo",
    (_message.Message,),
    {
        "DESCRIPTOR": _VERSIONINFO,
        "__module__": "temporal.api.version.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.version.v1.VersionInfo)
    },
)
_sym_db.RegisterMessage(VersionInfo)

if _descriptor._USE_C_DESCRIPTORS == False:

    DESCRIPTOR._options = None
    DESCRIPTOR._serialized_options = b"\n\032io.temporal.api.version.v1B\014MessageProtoP\001Z%go.temporal.io/api/version/v1;version\252\002\027Temporal.Api.Version.V1\352\002\032Temporal::Api::Version::V1"
    _RELEASEINFO.fields_by_name["release_time"]._options = None
    _RELEASEINFO.fields_by_name[
        "release_time"
    ]._serialized_options = b"\220\337\037\001"
    _VERSIONINFO.fields_by_name["last_update_time"]._options = None
    _VERSIONINFO.fields_by_name[
        "last_update_time"
    ]._serialized_options = b"\220\337\037\001"
    _RELEASEINFO._serialized_start = 170
    _RELEASEINFO._serialized_end = 271
    _ALERT._serialized_start = 273
    _ALERT._serialized_end = 348
    _VERSIONINFO._serialized_start = 351
    _VERSIONINFO._serialized_end = 608
# @@protoc_insertion_point(module_scope)
