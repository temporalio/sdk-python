"""
@generated by mypy-protobuf.  Do not edit manually!
isort:skip_file
"""
import builtins
import google.protobuf.descriptor
import google.protobuf.internal.containers
import google.protobuf.message
import google.protobuf.timestamp_pb2
import temporalio.api.enums.v1.cluster_pb2
import temporalio.api.enums.v1.common_pb2
import temporalio.api.version.v1.message_pb2
import typing
import typing_extensions

DESCRIPTOR: google.protobuf.descriptor.FileDescriptor

class ClusterMetadata(google.protobuf.message.Message):
    """data column"""

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    class IndexSearchAttributesEntry(google.protobuf.message.Message):
        DESCRIPTOR: google.protobuf.descriptor.Descriptor
        KEY_FIELD_NUMBER: builtins.int
        VALUE_FIELD_NUMBER: builtins.int
        key: typing.Text
        @property
        def value(self) -> global___IndexSearchAttributes: ...
        def __init__(
            self,
            *,
            key: typing.Text = ...,
            value: typing.Optional[global___IndexSearchAttributes] = ...,
        ) -> None: ...
        def HasField(
            self, field_name: typing_extensions.Literal["value", b"value"]
        ) -> builtins.bool: ...
        def ClearField(
            self,
            field_name: typing_extensions.Literal["key", b"key", "value", b"value"],
        ) -> None: ...

    CLUSTER_FIELD_NUMBER: builtins.int
    HISTORY_SHARD_COUNT_FIELD_NUMBER: builtins.int
    CLUSTER_ID_FIELD_NUMBER: builtins.int
    VERSION_INFO_FIELD_NUMBER: builtins.int
    INDEX_SEARCH_ATTRIBUTES_FIELD_NUMBER: builtins.int
    CLUSTER_ADDRESS_FIELD_NUMBER: builtins.int
    FAILOVER_VERSION_INCREMENT_FIELD_NUMBER: builtins.int
    INITIAL_FAILOVER_VERSION_FIELD_NUMBER: builtins.int
    IS_GLOBAL_NAMESPACE_ENABLED_FIELD_NUMBER: builtins.int
    IS_CONNECTION_ENABLED_FIELD_NUMBER: builtins.int
    cluster: typing.Text
    history_shard_count: builtins.int
    cluster_id: typing.Text
    @property
    def version_info(self) -> temporalio.api.version.v1.message_pb2.VersionInfo: ...
    @property
    def index_search_attributes(
        self,
    ) -> google.protobuf.internal.containers.MessageMap[
        typing.Text, global___IndexSearchAttributes
    ]: ...
    cluster_address: typing.Text
    failover_version_increment: builtins.int
    initial_failover_version: builtins.int
    is_global_namespace_enabled: builtins.bool
    is_connection_enabled: builtins.bool
    def __init__(
        self,
        *,
        cluster: typing.Text = ...,
        history_shard_count: builtins.int = ...,
        cluster_id: typing.Text = ...,
        version_info: typing.Optional[
            temporalio.api.version.v1.message_pb2.VersionInfo
        ] = ...,
        index_search_attributes: typing.Optional[
            typing.Mapping[typing.Text, global___IndexSearchAttributes]
        ] = ...,
        cluster_address: typing.Text = ...,
        failover_version_increment: builtins.int = ...,
        initial_failover_version: builtins.int = ...,
        is_global_namespace_enabled: builtins.bool = ...,
        is_connection_enabled: builtins.bool = ...,
    ) -> None: ...
    def HasField(
        self, field_name: typing_extensions.Literal["version_info", b"version_info"]
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "cluster",
            b"cluster",
            "cluster_address",
            b"cluster_address",
            "cluster_id",
            b"cluster_id",
            "failover_version_increment",
            b"failover_version_increment",
            "history_shard_count",
            b"history_shard_count",
            "index_search_attributes",
            b"index_search_attributes",
            "initial_failover_version",
            b"initial_failover_version",
            "is_connection_enabled",
            b"is_connection_enabled",
            "is_global_namespace_enabled",
            b"is_global_namespace_enabled",
            "version_info",
            b"version_info",
        ],
    ) -> None: ...

global___ClusterMetadata = ClusterMetadata

class IndexSearchAttributes(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    class CustomSearchAttributesEntry(google.protobuf.message.Message):
        DESCRIPTOR: google.protobuf.descriptor.Descriptor
        KEY_FIELD_NUMBER: builtins.int
        VALUE_FIELD_NUMBER: builtins.int
        key: typing.Text
        value: temporalio.api.enums.v1.common_pb2.IndexedValueType.ValueType
        def __init__(
            self,
            *,
            key: typing.Text = ...,
            value: temporalio.api.enums.v1.common_pb2.IndexedValueType.ValueType = ...,
        ) -> None: ...
        def ClearField(
            self,
            field_name: typing_extensions.Literal["key", b"key", "value", b"value"],
        ) -> None: ...

    CUSTOM_SEARCH_ATTRIBUTES_FIELD_NUMBER: builtins.int
    @property
    def custom_search_attributes(
        self,
    ) -> google.protobuf.internal.containers.ScalarMap[
        typing.Text, temporalio.api.enums.v1.common_pb2.IndexedValueType.ValueType
    ]: ...
    def __init__(
        self,
        *,
        custom_search_attributes: typing.Optional[
            typing.Mapping[
                typing.Text,
                temporalio.api.enums.v1.common_pb2.IndexedValueType.ValueType,
            ]
        ] = ...,
    ) -> None: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "custom_search_attributes", b"custom_search_attributes"
        ],
    ) -> None: ...

global___IndexSearchAttributes = IndexSearchAttributes

class HostInfo(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor
    IDENTITY_FIELD_NUMBER: builtins.int
    identity: typing.Text
    def __init__(
        self,
        *,
        identity: typing.Text = ...,
    ) -> None: ...
    def ClearField(
        self, field_name: typing_extensions.Literal["identity", b"identity"]
    ) -> None: ...

global___HostInfo = HostInfo

class RingInfo(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor
    ROLE_FIELD_NUMBER: builtins.int
    MEMBER_COUNT_FIELD_NUMBER: builtins.int
    MEMBERS_FIELD_NUMBER: builtins.int
    role: typing.Text
    member_count: builtins.int
    @property
    def members(
        self,
    ) -> google.protobuf.internal.containers.RepeatedCompositeFieldContainer[
        global___HostInfo
    ]: ...
    def __init__(
        self,
        *,
        role: typing.Text = ...,
        member_count: builtins.int = ...,
        members: typing.Optional[typing.Iterable[global___HostInfo]] = ...,
    ) -> None: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "member_count", b"member_count", "members", b"members", "role", b"role"
        ],
    ) -> None: ...

global___RingInfo = RingInfo

class MembershipInfo(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor
    CURRENT_HOST_FIELD_NUMBER: builtins.int
    REACHABLE_MEMBERS_FIELD_NUMBER: builtins.int
    RINGS_FIELD_NUMBER: builtins.int
    @property
    def current_host(self) -> global___HostInfo: ...
    @property
    def reachable_members(
        self,
    ) -> google.protobuf.internal.containers.RepeatedScalarFieldContainer[
        typing.Text
    ]: ...
    @property
    def rings(
        self,
    ) -> google.protobuf.internal.containers.RepeatedCompositeFieldContainer[
        global___RingInfo
    ]: ...
    def __init__(
        self,
        *,
        current_host: typing.Optional[global___HostInfo] = ...,
        reachable_members: typing.Optional[typing.Iterable[typing.Text]] = ...,
        rings: typing.Optional[typing.Iterable[global___RingInfo]] = ...,
    ) -> None: ...
    def HasField(
        self, field_name: typing_extensions.Literal["current_host", b"current_host"]
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "current_host",
            b"current_host",
            "reachable_members",
            b"reachable_members",
            "rings",
            b"rings",
        ],
    ) -> None: ...

global___MembershipInfo = MembershipInfo

class ClusterMember(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor
    ROLE_FIELD_NUMBER: builtins.int
    HOST_ID_FIELD_NUMBER: builtins.int
    RPC_ADDRESS_FIELD_NUMBER: builtins.int
    RPC_PORT_FIELD_NUMBER: builtins.int
    SESSION_START_TIME_FIELD_NUMBER: builtins.int
    LAST_HEARTBIT_TIME_FIELD_NUMBER: builtins.int
    RECORD_EXPIRY_TIME_FIELD_NUMBER: builtins.int
    role: temporalio.api.enums.v1.cluster_pb2.ClusterMemberRole.ValueType
    host_id: typing.Text
    rpc_address: typing.Text
    rpc_port: builtins.int
    @property
    def session_start_time(self) -> google.protobuf.timestamp_pb2.Timestamp: ...
    @property
    def last_heartbit_time(self) -> google.protobuf.timestamp_pb2.Timestamp: ...
    @property
    def record_expiry_time(self) -> google.protobuf.timestamp_pb2.Timestamp: ...
    def __init__(
        self,
        *,
        role: temporalio.api.enums.v1.cluster_pb2.ClusterMemberRole.ValueType = ...,
        host_id: typing.Text = ...,
        rpc_address: typing.Text = ...,
        rpc_port: builtins.int = ...,
        session_start_time: typing.Optional[
            google.protobuf.timestamp_pb2.Timestamp
        ] = ...,
        last_heartbit_time: typing.Optional[
            google.protobuf.timestamp_pb2.Timestamp
        ] = ...,
        record_expiry_time: typing.Optional[
            google.protobuf.timestamp_pb2.Timestamp
        ] = ...,
    ) -> None: ...
    def HasField(
        self,
        field_name: typing_extensions.Literal[
            "last_heartbit_time",
            b"last_heartbit_time",
            "record_expiry_time",
            b"record_expiry_time",
            "session_start_time",
            b"session_start_time",
        ],
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "host_id",
            b"host_id",
            "last_heartbit_time",
            b"last_heartbit_time",
            "record_expiry_time",
            b"record_expiry_time",
            "role",
            b"role",
            "rpc_address",
            b"rpc_address",
            "rpc_port",
            b"rpc_port",
            "session_start_time",
            b"session_start_time",
        ],
    ) -> None: ...

global___ClusterMember = ClusterMember
