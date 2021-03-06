"""
@generated by mypy-protobuf.  Do not edit manually!
isort:skip_file
"""
import builtins
import google.protobuf.descriptor
import google.protobuf.duration_pb2
import google.protobuf.internal.containers
import google.protobuf.message
import google.protobuf.timestamp_pb2
import temporalio.api.enums.v1.namespace_pb2
import typing
import typing_extensions

DESCRIPTOR: google.protobuf.descriptor.FileDescriptor

class NamespaceInfo(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    class DataEntry(google.protobuf.message.Message):
        DESCRIPTOR: google.protobuf.descriptor.Descriptor
        KEY_FIELD_NUMBER: builtins.int
        VALUE_FIELD_NUMBER: builtins.int
        key: typing.Text
        value: typing.Text
        def __init__(
            self,
            *,
            key: typing.Text = ...,
            value: typing.Text = ...,
        ) -> None: ...
        def ClearField(
            self,
            field_name: typing_extensions.Literal["key", b"key", "value", b"value"],
        ) -> None: ...

    NAME_FIELD_NUMBER: builtins.int
    STATE_FIELD_NUMBER: builtins.int
    DESCRIPTION_FIELD_NUMBER: builtins.int
    OWNER_EMAIL_FIELD_NUMBER: builtins.int
    DATA_FIELD_NUMBER: builtins.int
    ID_FIELD_NUMBER: builtins.int
    SUPPORTS_SCHEDULES_FIELD_NUMBER: builtins.int
    name: typing.Text
    state: temporalio.api.enums.v1.namespace_pb2.NamespaceState.ValueType
    description: typing.Text
    owner_email: typing.Text
    @property
    def data(
        self,
    ) -> google.protobuf.internal.containers.ScalarMap[typing.Text, typing.Text]:
        """A key-value map for any customized purpose."""
        pass
    id: typing.Text
    supports_schedules: builtins.bool
    """Whether scheduled workflows are supported on this namespace. This is only needed
    temporarily while the feature is experimental, so we can give it a high tag.
    """

    def __init__(
        self,
        *,
        name: typing.Text = ...,
        state: temporalio.api.enums.v1.namespace_pb2.NamespaceState.ValueType = ...,
        description: typing.Text = ...,
        owner_email: typing.Text = ...,
        data: typing.Optional[typing.Mapping[typing.Text, typing.Text]] = ...,
        id: typing.Text = ...,
        supports_schedules: builtins.bool = ...,
    ) -> None: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "data",
            b"data",
            "description",
            b"description",
            "id",
            b"id",
            "name",
            b"name",
            "owner_email",
            b"owner_email",
            "state",
            b"state",
            "supports_schedules",
            b"supports_schedules",
        ],
    ) -> None: ...

global___NamespaceInfo = NamespaceInfo

class NamespaceConfig(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor
    WORKFLOW_EXECUTION_RETENTION_TTL_FIELD_NUMBER: builtins.int
    BAD_BINARIES_FIELD_NUMBER: builtins.int
    HISTORY_ARCHIVAL_STATE_FIELD_NUMBER: builtins.int
    HISTORY_ARCHIVAL_URI_FIELD_NUMBER: builtins.int
    VISIBILITY_ARCHIVAL_STATE_FIELD_NUMBER: builtins.int
    VISIBILITY_ARCHIVAL_URI_FIELD_NUMBER: builtins.int
    @property
    def workflow_execution_retention_ttl(
        self,
    ) -> google.protobuf.duration_pb2.Duration: ...
    @property
    def bad_binaries(self) -> global___BadBinaries: ...
    history_archival_state: temporalio.api.enums.v1.namespace_pb2.ArchivalState.ValueType
    """If unspecified (ARCHIVAL_STATE_UNSPECIFIED) then default server configuration is used."""

    history_archival_uri: typing.Text
    visibility_archival_state: temporalio.api.enums.v1.namespace_pb2.ArchivalState.ValueType
    """If unspecified (ARCHIVAL_STATE_UNSPECIFIED) then default server configuration is used."""

    visibility_archival_uri: typing.Text
    def __init__(
        self,
        *,
        workflow_execution_retention_ttl: typing.Optional[
            google.protobuf.duration_pb2.Duration
        ] = ...,
        bad_binaries: typing.Optional[global___BadBinaries] = ...,
        history_archival_state: temporalio.api.enums.v1.namespace_pb2.ArchivalState.ValueType = ...,
        history_archival_uri: typing.Text = ...,
        visibility_archival_state: temporalio.api.enums.v1.namespace_pb2.ArchivalState.ValueType = ...,
        visibility_archival_uri: typing.Text = ...,
    ) -> None: ...
    def HasField(
        self,
        field_name: typing_extensions.Literal[
            "bad_binaries",
            b"bad_binaries",
            "workflow_execution_retention_ttl",
            b"workflow_execution_retention_ttl",
        ],
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "bad_binaries",
            b"bad_binaries",
            "history_archival_state",
            b"history_archival_state",
            "history_archival_uri",
            b"history_archival_uri",
            "visibility_archival_state",
            b"visibility_archival_state",
            "visibility_archival_uri",
            b"visibility_archival_uri",
            "workflow_execution_retention_ttl",
            b"workflow_execution_retention_ttl",
        ],
    ) -> None: ...

global___NamespaceConfig = NamespaceConfig

class BadBinaries(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    class BinariesEntry(google.protobuf.message.Message):
        DESCRIPTOR: google.protobuf.descriptor.Descriptor
        KEY_FIELD_NUMBER: builtins.int
        VALUE_FIELD_NUMBER: builtins.int
        key: typing.Text
        @property
        def value(self) -> global___BadBinaryInfo: ...
        def __init__(
            self,
            *,
            key: typing.Text = ...,
            value: typing.Optional[global___BadBinaryInfo] = ...,
        ) -> None: ...
        def HasField(
            self, field_name: typing_extensions.Literal["value", b"value"]
        ) -> builtins.bool: ...
        def ClearField(
            self,
            field_name: typing_extensions.Literal["key", b"key", "value", b"value"],
        ) -> None: ...

    BINARIES_FIELD_NUMBER: builtins.int
    @property
    def binaries(
        self,
    ) -> google.protobuf.internal.containers.MessageMap[
        typing.Text, global___BadBinaryInfo
    ]: ...
    def __init__(
        self,
        *,
        binaries: typing.Optional[
            typing.Mapping[typing.Text, global___BadBinaryInfo]
        ] = ...,
    ) -> None: ...
    def ClearField(
        self, field_name: typing_extensions.Literal["binaries", b"binaries"]
    ) -> None: ...

global___BadBinaries = BadBinaries

class BadBinaryInfo(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor
    REASON_FIELD_NUMBER: builtins.int
    OPERATOR_FIELD_NUMBER: builtins.int
    CREATE_TIME_FIELD_NUMBER: builtins.int
    reason: typing.Text
    operator: typing.Text
    @property
    def create_time(self) -> google.protobuf.timestamp_pb2.Timestamp: ...
    def __init__(
        self,
        *,
        reason: typing.Text = ...,
        operator: typing.Text = ...,
        create_time: typing.Optional[google.protobuf.timestamp_pb2.Timestamp] = ...,
    ) -> None: ...
    def HasField(
        self, field_name: typing_extensions.Literal["create_time", b"create_time"]
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "create_time", b"create_time", "operator", b"operator", "reason", b"reason"
        ],
    ) -> None: ...

global___BadBinaryInfo = BadBinaryInfo

class UpdateNamespaceInfo(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    class DataEntry(google.protobuf.message.Message):
        DESCRIPTOR: google.protobuf.descriptor.Descriptor
        KEY_FIELD_NUMBER: builtins.int
        VALUE_FIELD_NUMBER: builtins.int
        key: typing.Text
        value: typing.Text
        def __init__(
            self,
            *,
            key: typing.Text = ...,
            value: typing.Text = ...,
        ) -> None: ...
        def ClearField(
            self,
            field_name: typing_extensions.Literal["key", b"key", "value", b"value"],
        ) -> None: ...

    DESCRIPTION_FIELD_NUMBER: builtins.int
    OWNER_EMAIL_FIELD_NUMBER: builtins.int
    DATA_FIELD_NUMBER: builtins.int
    STATE_FIELD_NUMBER: builtins.int
    description: typing.Text
    owner_email: typing.Text
    @property
    def data(
        self,
    ) -> google.protobuf.internal.containers.ScalarMap[typing.Text, typing.Text]:
        """A key-value map for any customized purpose.
        If data already exists on the namespace,
        this will merge with the existing key values.
        """
        pass
    state: temporalio.api.enums.v1.namespace_pb2.NamespaceState.ValueType
    """New namespace state, server will reject if transition is not allowed.
    Allowed transitions are:
     Registered -> [ Deleted | Deprecated | Handover ]
     Handover -> [ Registered ]
    Default is NAMESPACE_STATE_UNSPECIFIED which is do not change state.
    """

    def __init__(
        self,
        *,
        description: typing.Text = ...,
        owner_email: typing.Text = ...,
        data: typing.Optional[typing.Mapping[typing.Text, typing.Text]] = ...,
        state: temporalio.api.enums.v1.namespace_pb2.NamespaceState.ValueType = ...,
    ) -> None: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "data",
            b"data",
            "description",
            b"description",
            "owner_email",
            b"owner_email",
            "state",
            b"state",
        ],
    ) -> None: ...

global___UpdateNamespaceInfo = UpdateNamespaceInfo

class NamespaceFilter(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor
    INCLUDE_DELETED_FIELD_NUMBER: builtins.int
    include_deleted: builtins.bool
    """By default namespaces in NAMESPACE_STATE_DELETED state are not included.
    Setting include_deleted to true will include deleted namespaces.
    Note: Namespace is in NAMESPACE_STATE_DELETED state when it was deleted from the system but associated data is not deleted yet.
    """

    def __init__(
        self,
        *,
        include_deleted: builtins.bool = ...,
    ) -> None: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal["include_deleted", b"include_deleted"],
    ) -> None: ...

global___NamespaceFilter = NamespaceFilter
