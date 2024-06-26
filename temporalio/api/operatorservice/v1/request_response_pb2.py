# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: temporal/api/operatorservice/v1/request_response.proto
"""Generated protocol buffer code."""

from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection
from google.protobuf import symbol_database as _symbol_database

# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()


from google.protobuf import duration_pb2 as google_dot_protobuf_dot_duration__pb2

from temporalio.api.enums.v1 import (
    common_pb2 as temporal_dot_api_dot_enums_dot_v1_dot_common__pb2,
)
from temporalio.api.nexus.v1 import (
    message_pb2 as temporal_dot_api_dot_nexus_dot_v1_dot_message__pb2,
)

DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(
    b'\n6temporal/api/operatorservice/v1/request_response.proto\x12\x1ftemporal.api.operatorservice.v1\x1a"temporal/api/enums/v1/common.proto\x1a#temporal/api/nexus/v1/message.proto\x1a\x1egoogle/protobuf/duration.proto"\xff\x01\n\x1a\x41\x64\x64SearchAttributesRequest\x12l\n\x11search_attributes\x18\x01 \x03(\x0b\x32Q.temporal.api.operatorservice.v1.AddSearchAttributesRequest.SearchAttributesEntry\x12\x11\n\tnamespace\x18\x02 \x01(\t\x1a`\n\x15SearchAttributesEntry\x12\x0b\n\x03key\x18\x01 \x01(\t\x12\x36\n\x05value\x18\x02 \x01(\x0e\x32\'.temporal.api.enums.v1.IndexedValueType:\x02\x38\x01"\x1d\n\x1b\x41\x64\x64SearchAttributesResponse"M\n\x1dRemoveSearchAttributesRequest\x12\x19\n\x11search_attributes\x18\x01 \x03(\t\x12\x11\n\tnamespace\x18\x02 \x01(\t" \n\x1eRemoveSearchAttributesResponse"0\n\x1bListSearchAttributesRequest\x12\x11\n\tnamespace\x18\x01 \x01(\t"\xe2\x04\n\x1cListSearchAttributesResponse\x12n\n\x11\x63ustom_attributes\x18\x01 \x03(\x0b\x32S.temporal.api.operatorservice.v1.ListSearchAttributesResponse.CustomAttributesEntry\x12n\n\x11system_attributes\x18\x02 \x03(\x0b\x32S.temporal.api.operatorservice.v1.ListSearchAttributesResponse.SystemAttributesEntry\x12h\n\x0estorage_schema\x18\x03 \x03(\x0b\x32P.temporal.api.operatorservice.v1.ListSearchAttributesResponse.StorageSchemaEntry\x1a`\n\x15\x43ustomAttributesEntry\x12\x0b\n\x03key\x18\x01 \x01(\t\x12\x36\n\x05value\x18\x02 \x01(\x0e\x32\'.temporal.api.enums.v1.IndexedValueType:\x02\x38\x01\x1a`\n\x15SystemAttributesEntry\x12\x0b\n\x03key\x18\x01 \x01(\t\x12\x36\n\x05value\x18\x02 \x01(\x0e\x32\'.temporal.api.enums.v1.IndexedValueType:\x02\x38\x01\x1a\x34\n\x12StorageSchemaEntry\x12\x0b\n\x03key\x18\x01 \x01(\t\x12\r\n\x05value\x18\x02 \x01(\t:\x02\x38\x01"|\n\x16\x44\x65leteNamespaceRequest\x12\x11\n\tnamespace\x18\x01 \x01(\t\x12\x14\n\x0cnamespace_id\x18\x02 \x01(\t\x12\x39\n\x16namespace_delete_delay\x18\x03 \x01(\x0b\x32\x19.google.protobuf.Duration"4\n\x17\x44\x65leteNamespaceResponse\x12\x19\n\x11\x64\x65leted_namespace\x18\x01 \x01(\t"\x84\x01\n\x1f\x41\x64\x64OrUpdateRemoteClusterRequest\x12\x18\n\x10\x66rontend_address\x18\x01 \x01(\t\x12(\n enable_remote_cluster_connection\x18\x02 \x01(\x08\x12\x1d\n\x15\x66rontend_http_address\x18\x03 \x01(\t""\n AddOrUpdateRemoteClusterResponse"2\n\x1aRemoveRemoteClusterRequest\x12\x14\n\x0c\x63luster_name\x18\x01 \x01(\t"\x1d\n\x1bRemoveRemoteClusterResponse"A\n\x13ListClustersRequest\x12\x11\n\tpage_size\x18\x01 \x01(\x05\x12\x17\n\x0fnext_page_token\x18\x02 \x01(\x0c"s\n\x14ListClustersResponse\x12\x42\n\x08\x63lusters\x18\x01 \x03(\x0b\x32\x30.temporal.api.operatorservice.v1.ClusterMetadata\x12\x17\n\x0fnext_page_token\x18\x04 \x01(\x0c"\xc0\x01\n\x0f\x43lusterMetadata\x12\x14\n\x0c\x63luster_name\x18\x01 \x01(\t\x12\x12\n\ncluster_id\x18\x02 \x01(\t\x12\x0f\n\x07\x61\x64\x64ress\x18\x03 \x01(\t\x12\x14\n\x0chttp_address\x18\x07 \x01(\t\x12 \n\x18initial_failover_version\x18\x04 \x01(\x03\x12\x1b\n\x13history_shard_count\x18\x05 \x01(\x05\x12\x1d\n\x15is_connection_enabled\x18\x06 \x01(\x08"%\n\x17GetNexusEndpointRequest\x12\n\n\x02id\x18\x01 \x01(\t"M\n\x18GetNexusEndpointResponse\x12\x31\n\x08\x65ndpoint\x18\x01 \x01(\x0b\x32\x1f.temporal.api.nexus.v1.Endpoint"O\n\x1a\x43reateNexusEndpointRequest\x12\x31\n\x04spec\x18\x01 \x01(\x0b\x32#.temporal.api.nexus.v1.EndpointSpec"P\n\x1b\x43reateNexusEndpointResponse\x12\x31\n\x08\x65ndpoint\x18\x01 \x01(\x0b\x32\x1f.temporal.api.nexus.v1.Endpoint"l\n\x1aUpdateNexusEndpointRequest\x12\n\n\x02id\x18\x01 \x01(\t\x12\x0f\n\x07version\x18\x02 \x01(\x03\x12\x31\n\x04spec\x18\x03 \x01(\x0b\x32#.temporal.api.nexus.v1.EndpointSpec"P\n\x1bUpdateNexusEndpointResponse\x12\x31\n\x08\x65ndpoint\x18\x01 \x01(\x0b\x32\x1f.temporal.api.nexus.v1.Endpoint"9\n\x1a\x44\x65leteNexusEndpointRequest\x12\n\n\x02id\x18\x01 \x01(\t\x12\x0f\n\x07version\x18\x02 \x01(\x03"\x1d\n\x1b\x44\x65leteNexusEndpointResponse"U\n\x19ListNexusEndpointsRequest\x12\x11\n\tpage_size\x18\x01 \x01(\x05\x12\x17\n\x0fnext_page_token\x18\x02 \x01(\x0c\x12\x0c\n\x04name\x18\x03 \x01(\t"i\n\x1aListNexusEndpointsResponse\x12\x17\n\x0fnext_page_token\x18\x01 \x01(\x0c\x12\x32\n\tendpoints\x18\x02 \x03(\x0b\x32\x1f.temporal.api.nexus.v1.EndpointB\xbe\x01\n"io.temporal.api.operatorservice.v1B\x14RequestResponseProtoP\x01Z5go.temporal.io/api/operatorservice/v1;operatorservice\xaa\x02!Temporalio.Api.OperatorService.V1\xea\x02$Temporalio::Api::OperatorService::V1b\x06proto3'
)


_ADDSEARCHATTRIBUTESREQUEST = DESCRIPTOR.message_types_by_name[
    "AddSearchAttributesRequest"
]
_ADDSEARCHATTRIBUTESREQUEST_SEARCHATTRIBUTESENTRY = (
    _ADDSEARCHATTRIBUTESREQUEST.nested_types_by_name["SearchAttributesEntry"]
)
_ADDSEARCHATTRIBUTESRESPONSE = DESCRIPTOR.message_types_by_name[
    "AddSearchAttributesResponse"
]
_REMOVESEARCHATTRIBUTESREQUEST = DESCRIPTOR.message_types_by_name[
    "RemoveSearchAttributesRequest"
]
_REMOVESEARCHATTRIBUTESRESPONSE = DESCRIPTOR.message_types_by_name[
    "RemoveSearchAttributesResponse"
]
_LISTSEARCHATTRIBUTESREQUEST = DESCRIPTOR.message_types_by_name[
    "ListSearchAttributesRequest"
]
_LISTSEARCHATTRIBUTESRESPONSE = DESCRIPTOR.message_types_by_name[
    "ListSearchAttributesResponse"
]
_LISTSEARCHATTRIBUTESRESPONSE_CUSTOMATTRIBUTESENTRY = (
    _LISTSEARCHATTRIBUTESRESPONSE.nested_types_by_name["CustomAttributesEntry"]
)
_LISTSEARCHATTRIBUTESRESPONSE_SYSTEMATTRIBUTESENTRY = (
    _LISTSEARCHATTRIBUTESRESPONSE.nested_types_by_name["SystemAttributesEntry"]
)
_LISTSEARCHATTRIBUTESRESPONSE_STORAGESCHEMAENTRY = (
    _LISTSEARCHATTRIBUTESRESPONSE.nested_types_by_name["StorageSchemaEntry"]
)
_DELETENAMESPACEREQUEST = DESCRIPTOR.message_types_by_name["DeleteNamespaceRequest"]
_DELETENAMESPACERESPONSE = DESCRIPTOR.message_types_by_name["DeleteNamespaceResponse"]
_ADDORUPDATEREMOTECLUSTERREQUEST = DESCRIPTOR.message_types_by_name[
    "AddOrUpdateRemoteClusterRequest"
]
_ADDORUPDATEREMOTECLUSTERRESPONSE = DESCRIPTOR.message_types_by_name[
    "AddOrUpdateRemoteClusterResponse"
]
_REMOVEREMOTECLUSTERREQUEST = DESCRIPTOR.message_types_by_name[
    "RemoveRemoteClusterRequest"
]
_REMOVEREMOTECLUSTERRESPONSE = DESCRIPTOR.message_types_by_name[
    "RemoveRemoteClusterResponse"
]
_LISTCLUSTERSREQUEST = DESCRIPTOR.message_types_by_name["ListClustersRequest"]
_LISTCLUSTERSRESPONSE = DESCRIPTOR.message_types_by_name["ListClustersResponse"]
_CLUSTERMETADATA = DESCRIPTOR.message_types_by_name["ClusterMetadata"]
_GETNEXUSENDPOINTREQUEST = DESCRIPTOR.message_types_by_name["GetNexusEndpointRequest"]
_GETNEXUSENDPOINTRESPONSE = DESCRIPTOR.message_types_by_name["GetNexusEndpointResponse"]
_CREATENEXUSENDPOINTREQUEST = DESCRIPTOR.message_types_by_name[
    "CreateNexusEndpointRequest"
]
_CREATENEXUSENDPOINTRESPONSE = DESCRIPTOR.message_types_by_name[
    "CreateNexusEndpointResponse"
]
_UPDATENEXUSENDPOINTREQUEST = DESCRIPTOR.message_types_by_name[
    "UpdateNexusEndpointRequest"
]
_UPDATENEXUSENDPOINTRESPONSE = DESCRIPTOR.message_types_by_name[
    "UpdateNexusEndpointResponse"
]
_DELETENEXUSENDPOINTREQUEST = DESCRIPTOR.message_types_by_name[
    "DeleteNexusEndpointRequest"
]
_DELETENEXUSENDPOINTRESPONSE = DESCRIPTOR.message_types_by_name[
    "DeleteNexusEndpointResponse"
]
_LISTNEXUSENDPOINTSREQUEST = DESCRIPTOR.message_types_by_name[
    "ListNexusEndpointsRequest"
]
_LISTNEXUSENDPOINTSRESPONSE = DESCRIPTOR.message_types_by_name[
    "ListNexusEndpointsResponse"
]
AddSearchAttributesRequest = _reflection.GeneratedProtocolMessageType(
    "AddSearchAttributesRequest",
    (_message.Message,),
    {
        "SearchAttributesEntry": _reflection.GeneratedProtocolMessageType(
            "SearchAttributesEntry",
            (_message.Message,),
            {
                "DESCRIPTOR": _ADDSEARCHATTRIBUTESREQUEST_SEARCHATTRIBUTESENTRY,
                "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
                # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.AddSearchAttributesRequest.SearchAttributesEntry)
            },
        ),
        "DESCRIPTOR": _ADDSEARCHATTRIBUTESREQUEST,
        "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.AddSearchAttributesRequest)
    },
)
_sym_db.RegisterMessage(AddSearchAttributesRequest)
_sym_db.RegisterMessage(AddSearchAttributesRequest.SearchAttributesEntry)

AddSearchAttributesResponse = _reflection.GeneratedProtocolMessageType(
    "AddSearchAttributesResponse",
    (_message.Message,),
    {
        "DESCRIPTOR": _ADDSEARCHATTRIBUTESRESPONSE,
        "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.AddSearchAttributesResponse)
    },
)
_sym_db.RegisterMessage(AddSearchAttributesResponse)

RemoveSearchAttributesRequest = _reflection.GeneratedProtocolMessageType(
    "RemoveSearchAttributesRequest",
    (_message.Message,),
    {
        "DESCRIPTOR": _REMOVESEARCHATTRIBUTESREQUEST,
        "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.RemoveSearchAttributesRequest)
    },
)
_sym_db.RegisterMessage(RemoveSearchAttributesRequest)

RemoveSearchAttributesResponse = _reflection.GeneratedProtocolMessageType(
    "RemoveSearchAttributesResponse",
    (_message.Message,),
    {
        "DESCRIPTOR": _REMOVESEARCHATTRIBUTESRESPONSE,
        "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.RemoveSearchAttributesResponse)
    },
)
_sym_db.RegisterMessage(RemoveSearchAttributesResponse)

ListSearchAttributesRequest = _reflection.GeneratedProtocolMessageType(
    "ListSearchAttributesRequest",
    (_message.Message,),
    {
        "DESCRIPTOR": _LISTSEARCHATTRIBUTESREQUEST,
        "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.ListSearchAttributesRequest)
    },
)
_sym_db.RegisterMessage(ListSearchAttributesRequest)

ListSearchAttributesResponse = _reflection.GeneratedProtocolMessageType(
    "ListSearchAttributesResponse",
    (_message.Message,),
    {
        "CustomAttributesEntry": _reflection.GeneratedProtocolMessageType(
            "CustomAttributesEntry",
            (_message.Message,),
            {
                "DESCRIPTOR": _LISTSEARCHATTRIBUTESRESPONSE_CUSTOMATTRIBUTESENTRY,
                "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
                # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.ListSearchAttributesResponse.CustomAttributesEntry)
            },
        ),
        "SystemAttributesEntry": _reflection.GeneratedProtocolMessageType(
            "SystemAttributesEntry",
            (_message.Message,),
            {
                "DESCRIPTOR": _LISTSEARCHATTRIBUTESRESPONSE_SYSTEMATTRIBUTESENTRY,
                "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
                # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.ListSearchAttributesResponse.SystemAttributesEntry)
            },
        ),
        "StorageSchemaEntry": _reflection.GeneratedProtocolMessageType(
            "StorageSchemaEntry",
            (_message.Message,),
            {
                "DESCRIPTOR": _LISTSEARCHATTRIBUTESRESPONSE_STORAGESCHEMAENTRY,
                "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
                # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.ListSearchAttributesResponse.StorageSchemaEntry)
            },
        ),
        "DESCRIPTOR": _LISTSEARCHATTRIBUTESRESPONSE,
        "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.ListSearchAttributesResponse)
    },
)
_sym_db.RegisterMessage(ListSearchAttributesResponse)
_sym_db.RegisterMessage(ListSearchAttributesResponse.CustomAttributesEntry)
_sym_db.RegisterMessage(ListSearchAttributesResponse.SystemAttributesEntry)
_sym_db.RegisterMessage(ListSearchAttributesResponse.StorageSchemaEntry)

DeleteNamespaceRequest = _reflection.GeneratedProtocolMessageType(
    "DeleteNamespaceRequest",
    (_message.Message,),
    {
        "DESCRIPTOR": _DELETENAMESPACEREQUEST,
        "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.DeleteNamespaceRequest)
    },
)
_sym_db.RegisterMessage(DeleteNamespaceRequest)

DeleteNamespaceResponse = _reflection.GeneratedProtocolMessageType(
    "DeleteNamespaceResponse",
    (_message.Message,),
    {
        "DESCRIPTOR": _DELETENAMESPACERESPONSE,
        "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.DeleteNamespaceResponse)
    },
)
_sym_db.RegisterMessage(DeleteNamespaceResponse)

AddOrUpdateRemoteClusterRequest = _reflection.GeneratedProtocolMessageType(
    "AddOrUpdateRemoteClusterRequest",
    (_message.Message,),
    {
        "DESCRIPTOR": _ADDORUPDATEREMOTECLUSTERREQUEST,
        "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.AddOrUpdateRemoteClusterRequest)
    },
)
_sym_db.RegisterMessage(AddOrUpdateRemoteClusterRequest)

AddOrUpdateRemoteClusterResponse = _reflection.GeneratedProtocolMessageType(
    "AddOrUpdateRemoteClusterResponse",
    (_message.Message,),
    {
        "DESCRIPTOR": _ADDORUPDATEREMOTECLUSTERRESPONSE,
        "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.AddOrUpdateRemoteClusterResponse)
    },
)
_sym_db.RegisterMessage(AddOrUpdateRemoteClusterResponse)

RemoveRemoteClusterRequest = _reflection.GeneratedProtocolMessageType(
    "RemoveRemoteClusterRequest",
    (_message.Message,),
    {
        "DESCRIPTOR": _REMOVEREMOTECLUSTERREQUEST,
        "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.RemoveRemoteClusterRequest)
    },
)
_sym_db.RegisterMessage(RemoveRemoteClusterRequest)

RemoveRemoteClusterResponse = _reflection.GeneratedProtocolMessageType(
    "RemoveRemoteClusterResponse",
    (_message.Message,),
    {
        "DESCRIPTOR": _REMOVEREMOTECLUSTERRESPONSE,
        "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.RemoveRemoteClusterResponse)
    },
)
_sym_db.RegisterMessage(RemoveRemoteClusterResponse)

ListClustersRequest = _reflection.GeneratedProtocolMessageType(
    "ListClustersRequest",
    (_message.Message,),
    {
        "DESCRIPTOR": _LISTCLUSTERSREQUEST,
        "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.ListClustersRequest)
    },
)
_sym_db.RegisterMessage(ListClustersRequest)

ListClustersResponse = _reflection.GeneratedProtocolMessageType(
    "ListClustersResponse",
    (_message.Message,),
    {
        "DESCRIPTOR": _LISTCLUSTERSRESPONSE,
        "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.ListClustersResponse)
    },
)
_sym_db.RegisterMessage(ListClustersResponse)

ClusterMetadata = _reflection.GeneratedProtocolMessageType(
    "ClusterMetadata",
    (_message.Message,),
    {
        "DESCRIPTOR": _CLUSTERMETADATA,
        "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.ClusterMetadata)
    },
)
_sym_db.RegisterMessage(ClusterMetadata)

GetNexusEndpointRequest = _reflection.GeneratedProtocolMessageType(
    "GetNexusEndpointRequest",
    (_message.Message,),
    {
        "DESCRIPTOR": _GETNEXUSENDPOINTREQUEST,
        "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.GetNexusEndpointRequest)
    },
)
_sym_db.RegisterMessage(GetNexusEndpointRequest)

GetNexusEndpointResponse = _reflection.GeneratedProtocolMessageType(
    "GetNexusEndpointResponse",
    (_message.Message,),
    {
        "DESCRIPTOR": _GETNEXUSENDPOINTRESPONSE,
        "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.GetNexusEndpointResponse)
    },
)
_sym_db.RegisterMessage(GetNexusEndpointResponse)

CreateNexusEndpointRequest = _reflection.GeneratedProtocolMessageType(
    "CreateNexusEndpointRequest",
    (_message.Message,),
    {
        "DESCRIPTOR": _CREATENEXUSENDPOINTREQUEST,
        "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.CreateNexusEndpointRequest)
    },
)
_sym_db.RegisterMessage(CreateNexusEndpointRequest)

CreateNexusEndpointResponse = _reflection.GeneratedProtocolMessageType(
    "CreateNexusEndpointResponse",
    (_message.Message,),
    {
        "DESCRIPTOR": _CREATENEXUSENDPOINTRESPONSE,
        "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.CreateNexusEndpointResponse)
    },
)
_sym_db.RegisterMessage(CreateNexusEndpointResponse)

UpdateNexusEndpointRequest = _reflection.GeneratedProtocolMessageType(
    "UpdateNexusEndpointRequest",
    (_message.Message,),
    {
        "DESCRIPTOR": _UPDATENEXUSENDPOINTREQUEST,
        "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.UpdateNexusEndpointRequest)
    },
)
_sym_db.RegisterMessage(UpdateNexusEndpointRequest)

UpdateNexusEndpointResponse = _reflection.GeneratedProtocolMessageType(
    "UpdateNexusEndpointResponse",
    (_message.Message,),
    {
        "DESCRIPTOR": _UPDATENEXUSENDPOINTRESPONSE,
        "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.UpdateNexusEndpointResponse)
    },
)
_sym_db.RegisterMessage(UpdateNexusEndpointResponse)

DeleteNexusEndpointRequest = _reflection.GeneratedProtocolMessageType(
    "DeleteNexusEndpointRequest",
    (_message.Message,),
    {
        "DESCRIPTOR": _DELETENEXUSENDPOINTREQUEST,
        "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.DeleteNexusEndpointRequest)
    },
)
_sym_db.RegisterMessage(DeleteNexusEndpointRequest)

DeleteNexusEndpointResponse = _reflection.GeneratedProtocolMessageType(
    "DeleteNexusEndpointResponse",
    (_message.Message,),
    {
        "DESCRIPTOR": _DELETENEXUSENDPOINTRESPONSE,
        "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.DeleteNexusEndpointResponse)
    },
)
_sym_db.RegisterMessage(DeleteNexusEndpointResponse)

ListNexusEndpointsRequest = _reflection.GeneratedProtocolMessageType(
    "ListNexusEndpointsRequest",
    (_message.Message,),
    {
        "DESCRIPTOR": _LISTNEXUSENDPOINTSREQUEST,
        "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.ListNexusEndpointsRequest)
    },
)
_sym_db.RegisterMessage(ListNexusEndpointsRequest)

ListNexusEndpointsResponse = _reflection.GeneratedProtocolMessageType(
    "ListNexusEndpointsResponse",
    (_message.Message,),
    {
        "DESCRIPTOR": _LISTNEXUSENDPOINTSRESPONSE,
        "__module__": "temporal.api.operatorservice.v1.request_response_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.operatorservice.v1.ListNexusEndpointsResponse)
    },
)
_sym_db.RegisterMessage(ListNexusEndpointsResponse)

if _descriptor._USE_C_DESCRIPTORS == False:
    DESCRIPTOR._options = None
    DESCRIPTOR._serialized_options = b'\n"io.temporal.api.operatorservice.v1B\024RequestResponseProtoP\001Z5go.temporal.io/api/operatorservice/v1;operatorservice\252\002!Temporalio.Api.OperatorService.V1\352\002$Temporalio::Api::OperatorService::V1'
    _ADDSEARCHATTRIBUTESREQUEST_SEARCHATTRIBUTESENTRY._options = None
    _ADDSEARCHATTRIBUTESREQUEST_SEARCHATTRIBUTESENTRY._serialized_options = b"8\001"
    _LISTSEARCHATTRIBUTESRESPONSE_CUSTOMATTRIBUTESENTRY._options = None
    _LISTSEARCHATTRIBUTESRESPONSE_CUSTOMATTRIBUTESENTRY._serialized_options = b"8\001"
    _LISTSEARCHATTRIBUTESRESPONSE_SYSTEMATTRIBUTESENTRY._options = None
    _LISTSEARCHATTRIBUTESRESPONSE_SYSTEMATTRIBUTESENTRY._serialized_options = b"8\001"
    _LISTSEARCHATTRIBUTESRESPONSE_STORAGESCHEMAENTRY._options = None
    _LISTSEARCHATTRIBUTESRESPONSE_STORAGESCHEMAENTRY._serialized_options = b"8\001"
    _ADDSEARCHATTRIBUTESREQUEST._serialized_start = 197
    _ADDSEARCHATTRIBUTESREQUEST._serialized_end = 452
    _ADDSEARCHATTRIBUTESREQUEST_SEARCHATTRIBUTESENTRY._serialized_start = 356
    _ADDSEARCHATTRIBUTESREQUEST_SEARCHATTRIBUTESENTRY._serialized_end = 452
    _ADDSEARCHATTRIBUTESRESPONSE._serialized_start = 454
    _ADDSEARCHATTRIBUTESRESPONSE._serialized_end = 483
    _REMOVESEARCHATTRIBUTESREQUEST._serialized_start = 485
    _REMOVESEARCHATTRIBUTESREQUEST._serialized_end = 562
    _REMOVESEARCHATTRIBUTESRESPONSE._serialized_start = 564
    _REMOVESEARCHATTRIBUTESRESPONSE._serialized_end = 596
    _LISTSEARCHATTRIBUTESREQUEST._serialized_start = 598
    _LISTSEARCHATTRIBUTESREQUEST._serialized_end = 646
    _LISTSEARCHATTRIBUTESRESPONSE._serialized_start = 649
    _LISTSEARCHATTRIBUTESRESPONSE._serialized_end = 1259
    _LISTSEARCHATTRIBUTESRESPONSE_CUSTOMATTRIBUTESENTRY._serialized_start = 1011
    _LISTSEARCHATTRIBUTESRESPONSE_CUSTOMATTRIBUTESENTRY._serialized_end = 1107
    _LISTSEARCHATTRIBUTESRESPONSE_SYSTEMATTRIBUTESENTRY._serialized_start = 1109
    _LISTSEARCHATTRIBUTESRESPONSE_SYSTEMATTRIBUTESENTRY._serialized_end = 1205
    _LISTSEARCHATTRIBUTESRESPONSE_STORAGESCHEMAENTRY._serialized_start = 1207
    _LISTSEARCHATTRIBUTESRESPONSE_STORAGESCHEMAENTRY._serialized_end = 1259
    _DELETENAMESPACEREQUEST._serialized_start = 1261
    _DELETENAMESPACEREQUEST._serialized_end = 1385
    _DELETENAMESPACERESPONSE._serialized_start = 1387
    _DELETENAMESPACERESPONSE._serialized_end = 1439
    _ADDORUPDATEREMOTECLUSTERREQUEST._serialized_start = 1442
    _ADDORUPDATEREMOTECLUSTERREQUEST._serialized_end = 1574
    _ADDORUPDATEREMOTECLUSTERRESPONSE._serialized_start = 1576
    _ADDORUPDATEREMOTECLUSTERRESPONSE._serialized_end = 1610
    _REMOVEREMOTECLUSTERREQUEST._serialized_start = 1612
    _REMOVEREMOTECLUSTERREQUEST._serialized_end = 1662
    _REMOVEREMOTECLUSTERRESPONSE._serialized_start = 1664
    _REMOVEREMOTECLUSTERRESPONSE._serialized_end = 1693
    _LISTCLUSTERSREQUEST._serialized_start = 1695
    _LISTCLUSTERSREQUEST._serialized_end = 1760
    _LISTCLUSTERSRESPONSE._serialized_start = 1762
    _LISTCLUSTERSRESPONSE._serialized_end = 1877
    _CLUSTERMETADATA._serialized_start = 1880
    _CLUSTERMETADATA._serialized_end = 2072
    _GETNEXUSENDPOINTREQUEST._serialized_start = 2074
    _GETNEXUSENDPOINTREQUEST._serialized_end = 2111
    _GETNEXUSENDPOINTRESPONSE._serialized_start = 2113
    _GETNEXUSENDPOINTRESPONSE._serialized_end = 2190
    _CREATENEXUSENDPOINTREQUEST._serialized_start = 2192
    _CREATENEXUSENDPOINTREQUEST._serialized_end = 2271
    _CREATENEXUSENDPOINTRESPONSE._serialized_start = 2273
    _CREATENEXUSENDPOINTRESPONSE._serialized_end = 2353
    _UPDATENEXUSENDPOINTREQUEST._serialized_start = 2355
    _UPDATENEXUSENDPOINTREQUEST._serialized_end = 2463
    _UPDATENEXUSENDPOINTRESPONSE._serialized_start = 2465
    _UPDATENEXUSENDPOINTRESPONSE._serialized_end = 2545
    _DELETENEXUSENDPOINTREQUEST._serialized_start = 2547
    _DELETENEXUSENDPOINTREQUEST._serialized_end = 2604
    _DELETENEXUSENDPOINTRESPONSE._serialized_start = 2606
    _DELETENEXUSENDPOINTRESPONSE._serialized_end = 2635
    _LISTNEXUSENDPOINTSREQUEST._serialized_start = 2637
    _LISTNEXUSENDPOINTSREQUEST._serialized_end = 2722
    _LISTNEXUSENDPOINTSRESPONSE._serialized_start = 2724
    _LISTNEXUSENDPOINTSRESPONSE._serialized_end = 2829
# @@protoc_insertion_point(module_scope)
