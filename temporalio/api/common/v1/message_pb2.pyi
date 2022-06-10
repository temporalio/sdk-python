"""
@generated by mypy-protobuf.  Do not edit manually!
isort:skip_file
"""
import builtins
import google.protobuf.descriptor
import google.protobuf.duration_pb2
import google.protobuf.internal.containers
import google.protobuf.message
import temporalio.api.enums.v1.common_pb2
import typing
import typing_extensions

DESCRIPTOR: google.protobuf.descriptor.FileDescriptor

class DataBlob(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor
    ENCODING_TYPE_FIELD_NUMBER: builtins.int
    DATA_FIELD_NUMBER: builtins.int
    encoding_type: temporalio.api.enums.v1.common_pb2.EncodingType.ValueType
    data: builtins.bytes
    def __init__(
        self,
        *,
        encoding_type: temporalio.api.enums.v1.common_pb2.EncodingType.ValueType = ...,
        data: builtins.bytes = ...,
    ) -> None: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "data", b"data", "encoding_type", b"encoding_type"
        ],
    ) -> None: ...

global___DataBlob = DataBlob

class Payloads(google.protobuf.message.Message):
    """See `Payload`"""

    DESCRIPTOR: google.protobuf.descriptor.Descriptor
    PAYLOADS_FIELD_NUMBER: builtins.int
    @property
    def payloads(
        self,
    ) -> google.protobuf.internal.containers.RepeatedCompositeFieldContainer[
        global___Payload
    ]: ...
    def __init__(
        self,
        *,
        payloads: typing.Optional[typing.Iterable[global___Payload]] = ...,
    ) -> None: ...
    def ClearField(
        self, field_name: typing_extensions.Literal["payloads", b"payloads"]
    ) -> None: ...

global___Payloads = Payloads

class Payload(google.protobuf.message.Message):
    """Represents some binary (byte array) data (ex: activity input parameters or workflow result) with
    metadata which describes this binary data (format, encoding, encryption, etc). Serialization
    of the data may be user-defined.
    """

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    class MetadataEntry(google.protobuf.message.Message):
        DESCRIPTOR: google.protobuf.descriptor.Descriptor
        KEY_FIELD_NUMBER: builtins.int
        VALUE_FIELD_NUMBER: builtins.int
        key: typing.Text
        value: builtins.bytes
        def __init__(
            self,
            *,
            key: typing.Text = ...,
            value: builtins.bytes = ...,
        ) -> None: ...
        def ClearField(
            self,
            field_name: typing_extensions.Literal["key", b"key", "value", b"value"],
        ) -> None: ...

    METADATA_FIELD_NUMBER: builtins.int
    DATA_FIELD_NUMBER: builtins.int
    @property
    def metadata(
        self,
    ) -> google.protobuf.internal.containers.ScalarMap[typing.Text, builtins.bytes]: ...
    data: builtins.bytes
    def __init__(
        self,
        *,
        metadata: typing.Optional[typing.Mapping[typing.Text, builtins.bytes]] = ...,
        data: builtins.bytes = ...,
    ) -> None: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal["data", b"data", "metadata", b"metadata"],
    ) -> None: ...

global___Payload = Payload

class SearchAttributes(google.protobuf.message.Message):
    """A user-defined set of *indexed* fields that are used/exposed when listing/searching workflows.
    The payload is not serialized in a user-defined way.
    """

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    class IndexedFieldsEntry(google.protobuf.message.Message):
        DESCRIPTOR: google.protobuf.descriptor.Descriptor
        KEY_FIELD_NUMBER: builtins.int
        VALUE_FIELD_NUMBER: builtins.int
        key: typing.Text
        @property
        def value(self) -> global___Payload: ...
        def __init__(
            self,
            *,
            key: typing.Text = ...,
            value: typing.Optional[global___Payload] = ...,
        ) -> None: ...
        def HasField(
            self, field_name: typing_extensions.Literal["value", b"value"]
        ) -> builtins.bool: ...
        def ClearField(
            self,
            field_name: typing_extensions.Literal["key", b"key", "value", b"value"],
        ) -> None: ...

    INDEXED_FIELDS_FIELD_NUMBER: builtins.int
    @property
    def indexed_fields(
        self,
    ) -> google.protobuf.internal.containers.MessageMap[
        typing.Text, global___Payload
    ]: ...
    def __init__(
        self,
        *,
        indexed_fields: typing.Optional[
            typing.Mapping[typing.Text, global___Payload]
        ] = ...,
    ) -> None: ...
    def ClearField(
        self, field_name: typing_extensions.Literal["indexed_fields", b"indexed_fields"]
    ) -> None: ...

global___SearchAttributes = SearchAttributes

class Memo(google.protobuf.message.Message):
    """A user-defined set of *unindexed* fields that are exposed when listing/searching workflows"""

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    class FieldsEntry(google.protobuf.message.Message):
        DESCRIPTOR: google.protobuf.descriptor.Descriptor
        KEY_FIELD_NUMBER: builtins.int
        VALUE_FIELD_NUMBER: builtins.int
        key: typing.Text
        @property
        def value(self) -> global___Payload: ...
        def __init__(
            self,
            *,
            key: typing.Text = ...,
            value: typing.Optional[global___Payload] = ...,
        ) -> None: ...
        def HasField(
            self, field_name: typing_extensions.Literal["value", b"value"]
        ) -> builtins.bool: ...
        def ClearField(
            self,
            field_name: typing_extensions.Literal["key", b"key", "value", b"value"],
        ) -> None: ...

    FIELDS_FIELD_NUMBER: builtins.int
    @property
    def fields(
        self,
    ) -> google.protobuf.internal.containers.MessageMap[
        typing.Text, global___Payload
    ]: ...
    def __init__(
        self,
        *,
        fields: typing.Optional[typing.Mapping[typing.Text, global___Payload]] = ...,
    ) -> None: ...
    def ClearField(
        self, field_name: typing_extensions.Literal["fields", b"fields"]
    ) -> None: ...

global___Memo = Memo

class Header(google.protobuf.message.Message):
    """Contains metadata that can be attached to a variety of requests, like starting a workflow, and
    can be propagated between, for example, workflows and activities.
    """

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    class FieldsEntry(google.protobuf.message.Message):
        DESCRIPTOR: google.protobuf.descriptor.Descriptor
        KEY_FIELD_NUMBER: builtins.int
        VALUE_FIELD_NUMBER: builtins.int
        key: typing.Text
        @property
        def value(self) -> global___Payload: ...
        def __init__(
            self,
            *,
            key: typing.Text = ...,
            value: typing.Optional[global___Payload] = ...,
        ) -> None: ...
        def HasField(
            self, field_name: typing_extensions.Literal["value", b"value"]
        ) -> builtins.bool: ...
        def ClearField(
            self,
            field_name: typing_extensions.Literal["key", b"key", "value", b"value"],
        ) -> None: ...

    FIELDS_FIELD_NUMBER: builtins.int
    @property
    def fields(
        self,
    ) -> google.protobuf.internal.containers.MessageMap[
        typing.Text, global___Payload
    ]: ...
    def __init__(
        self,
        *,
        fields: typing.Optional[typing.Mapping[typing.Text, global___Payload]] = ...,
    ) -> None: ...
    def ClearField(
        self, field_name: typing_extensions.Literal["fields", b"fields"]
    ) -> None: ...

global___Header = Header

class WorkflowExecution(google.protobuf.message.Message):
    """Identifies a specific workflow within a namespace. Practically speaking, because run_id is a
    uuid, a workflow execution is globally unique. Note that many commands allow specifying an empty
    run id as a way of saying "target the latest run of the workflow".
    """

    DESCRIPTOR: google.protobuf.descriptor.Descriptor
    WORKFLOW_ID_FIELD_NUMBER: builtins.int
    RUN_ID_FIELD_NUMBER: builtins.int
    workflow_id: typing.Text
    run_id: typing.Text
    def __init__(
        self,
        *,
        workflow_id: typing.Text = ...,
        run_id: typing.Text = ...,
    ) -> None: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "run_id", b"run_id", "workflow_id", b"workflow_id"
        ],
    ) -> None: ...

global___WorkflowExecution = WorkflowExecution

class WorkflowType(google.protobuf.message.Message):
    """Represents the identifier used by a workflow author to define the workflow. Typically, the
    name of a function. This is sometimes referred to as the workflow's "name"
    """

    DESCRIPTOR: google.protobuf.descriptor.Descriptor
    NAME_FIELD_NUMBER: builtins.int
    name: typing.Text
    def __init__(
        self,
        *,
        name: typing.Text = ...,
    ) -> None: ...
    def ClearField(
        self, field_name: typing_extensions.Literal["name", b"name"]
    ) -> None: ...

global___WorkflowType = WorkflowType

class ActivityType(google.protobuf.message.Message):
    """Represents the identifier used by a activity author to define the activity. Typically, the
    name of a function. This is sometimes referred to as the activity's "name"
    """

    DESCRIPTOR: google.protobuf.descriptor.Descriptor
    NAME_FIELD_NUMBER: builtins.int
    name: typing.Text
    def __init__(
        self,
        *,
        name: typing.Text = ...,
    ) -> None: ...
    def ClearField(
        self, field_name: typing_extensions.Literal["name", b"name"]
    ) -> None: ...

global___ActivityType = ActivityType

class RetryPolicy(google.protobuf.message.Message):
    """How retries ought to be handled, usable by both workflows and activities"""

    DESCRIPTOR: google.protobuf.descriptor.Descriptor
    INITIAL_INTERVAL_FIELD_NUMBER: builtins.int
    BACKOFF_COEFFICIENT_FIELD_NUMBER: builtins.int
    MAXIMUM_INTERVAL_FIELD_NUMBER: builtins.int
    MAXIMUM_ATTEMPTS_FIELD_NUMBER: builtins.int
    NON_RETRYABLE_ERROR_TYPES_FIELD_NUMBER: builtins.int
    @property
    def initial_interval(self) -> google.protobuf.duration_pb2.Duration:
        """Interval of the first retry. If retryBackoffCoefficient is 1.0 then it is used for all retries."""
        pass
    backoff_coefficient: builtins.float
    """Coefficient used to calculate the next retry interval.
    The next retry interval is previous interval multiplied by the coefficient.
    Must be 1 or larger.
    """

    @property
    def maximum_interval(self) -> google.protobuf.duration_pb2.Duration:
        """Maximum interval between retries. Exponential backoff leads to interval increase.
        This value is the cap of the increase. Default is 100x of the initial interval.
        """
        pass
    maximum_attempts: builtins.int
    """Maximum number of attempts. When exceeded the retries stop even if not expired yet.
    1 disables retries. 0 means unlimited (up to the timeouts)
    """

    @property
    def non_retryable_error_types(
        self,
    ) -> google.protobuf.internal.containers.RepeatedScalarFieldContainer[typing.Text]:
        """Non-Retryable errors types. Will stop retrying if the error type matches this list. Note that
        this is not a substring match, the error *type* (not message) must match exactly.
        """
        pass
    def __init__(
        self,
        *,
        initial_interval: typing.Optional[google.protobuf.duration_pb2.Duration] = ...,
        backoff_coefficient: builtins.float = ...,
        maximum_interval: typing.Optional[google.protobuf.duration_pb2.Duration] = ...,
        maximum_attempts: builtins.int = ...,
        non_retryable_error_types: typing.Optional[typing.Iterable[typing.Text]] = ...,
    ) -> None: ...
    def HasField(
        self,
        field_name: typing_extensions.Literal[
            "initial_interval",
            b"initial_interval",
            "maximum_interval",
            b"maximum_interval",
        ],
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "backoff_coefficient",
            b"backoff_coefficient",
            "initial_interval",
            b"initial_interval",
            "maximum_attempts",
            b"maximum_attempts",
            "maximum_interval",
            b"maximum_interval",
            "non_retryable_error_types",
            b"non_retryable_error_types",
        ],
    ) -> None: ...

global___RetryPolicy = RetryPolicy
