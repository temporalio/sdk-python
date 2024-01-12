# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: temporal/api/replication/v1/message.proto
"""Generated protocol buffer code."""
from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection
from google.protobuf import symbol_database as _symbol_database

# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()


from google.protobuf import timestamp_pb2 as google_dot_protobuf_dot_timestamp__pb2

from temporalio.api.enums.v1 import (
    namespace_pb2 as temporal_dot_api_dot_enums_dot_v1_dot_namespace__pb2,
)

DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(
    b'\n)temporal/api/replication/v1/message.proto\x12\x1btemporal.api.replication.v1\x1a\x1fgoogle/protobuf/timestamp.proto\x1a%temporal/api/enums/v1/namespace.proto"0\n\x18\x43lusterReplicationConfig\x12\x14\n\x0c\x63luster_name\x18\x01 \x01(\t"\xba\x01\n\x1aNamespaceReplicationConfig\x12\x1b\n\x13\x61\x63tive_cluster_name\x18\x01 \x01(\t\x12G\n\x08\x63lusters\x18\x02 \x03(\x0b\x32\x35.temporal.api.replication.v1.ClusterReplicationConfig\x12\x36\n\x05state\x18\x03 \x01(\x0e\x32\'.temporal.api.enums.v1.ReplicationState"]\n\x0e\x46\x61iloverStatus\x12\x31\n\rfailover_time\x18\x01 \x01(\x0b\x32\x1a.google.protobuf.Timestamp\x12\x18\n\x10\x66\x61ilover_version\x18\x02 \x01(\x03\x42\xa2\x01\n\x1eio.temporal.api.replication.v1B\x0cMessageProtoP\x01Z-go.temporal.io/api/replication/v1;replication\xaa\x02\x1dTemporalio.Api.Replication.V1\xea\x02 Temporalio::Api::Replication::V1b\x06proto3'
)


_CLUSTERREPLICATIONCONFIG = DESCRIPTOR.message_types_by_name["ClusterReplicationConfig"]
_NAMESPACEREPLICATIONCONFIG = DESCRIPTOR.message_types_by_name[
    "NamespaceReplicationConfig"
]
_FAILOVERSTATUS = DESCRIPTOR.message_types_by_name["FailoverStatus"]
ClusterReplicationConfig = _reflection.GeneratedProtocolMessageType(
    "ClusterReplicationConfig",
    (_message.Message,),
    {
        "DESCRIPTOR": _CLUSTERREPLICATIONCONFIG,
        "__module__": "temporal.api.replication.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.replication.v1.ClusterReplicationConfig)
    },
)
_sym_db.RegisterMessage(ClusterReplicationConfig)

NamespaceReplicationConfig = _reflection.GeneratedProtocolMessageType(
    "NamespaceReplicationConfig",
    (_message.Message,),
    {
        "DESCRIPTOR": _NAMESPACEREPLICATIONCONFIG,
        "__module__": "temporal.api.replication.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.replication.v1.NamespaceReplicationConfig)
    },
)
_sym_db.RegisterMessage(NamespaceReplicationConfig)

FailoverStatus = _reflection.GeneratedProtocolMessageType(
    "FailoverStatus",
    (_message.Message,),
    {
        "DESCRIPTOR": _FAILOVERSTATUS,
        "__module__": "temporal.api.replication.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.replication.v1.FailoverStatus)
    },
)
_sym_db.RegisterMessage(FailoverStatus)

if _descriptor._USE_C_DESCRIPTORS == False:
    DESCRIPTOR._options = None
    DESCRIPTOR._serialized_options = b"\n\036io.temporal.api.replication.v1B\014MessageProtoP\001Z-go.temporal.io/api/replication/v1;replication\252\002\035Temporalio.Api.Replication.V1\352\002 Temporalio::Api::Replication::V1"
    _CLUSTERREPLICATIONCONFIG._serialized_start = 146
    _CLUSTERREPLICATIONCONFIG._serialized_end = 194
    _NAMESPACEREPLICATIONCONFIG._serialized_start = 197
    _NAMESPACEREPLICATIONCONFIG._serialized_end = 383
    _FAILOVERSTATUS._serialized_start = 385
    _FAILOVERSTATUS._serialized_end = 478
# @@protoc_insertion_point(module_scope)
