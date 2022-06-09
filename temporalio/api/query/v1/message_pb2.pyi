"""
@generated by mypy-protobuf.  Do not edit manually!
isort:skip_file
"""
import builtins
import google.protobuf.descriptor
import google.protobuf.message
import temporalio.api.common.v1.message_pb2
import temporalio.api.enums.v1.query_pb2
import temporalio.api.enums.v1.workflow_pb2
import typing
import typing_extensions

DESCRIPTOR: google.protobuf.descriptor.FileDescriptor

class WorkflowQuery(google.protobuf.message.Message):
    """See https://docs.temporal.io/docs/concepts/queries/"""

    DESCRIPTOR: google.protobuf.descriptor.Descriptor
    QUERY_TYPE_FIELD_NUMBER: builtins.int
    QUERY_ARGS_FIELD_NUMBER: builtins.int
    HEADER_FIELD_NUMBER: builtins.int
    query_type: typing.Text
    """The workflow-author-defined identifier of the query. Typically a function name."""
    @property
    def query_args(self) -> temporalio.api.common.v1.message_pb2.Payloads:
        """Serialized arguments that will be provided to the query handler."""
        pass
    @property
    def header(self) -> temporalio.api.common.v1.message_pb2.Header:
        """Headers that were passed by the caller of the query and copied by temporal
        server into the workflow task.
        """
        pass
    def __init__(
        self,
        *,
        query_type: typing.Text = ...,
        query_args: typing.Optional[
            temporalio.api.common.v1.message_pb2.Payloads
        ] = ...,
        header: typing.Optional[temporalio.api.common.v1.message_pb2.Header] = ...,
    ) -> None: ...
    def HasField(
        self,
        field_name: typing_extensions.Literal[
            "header", b"header", "query_args", b"query_args"
        ],
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "header",
            b"header",
            "query_args",
            b"query_args",
            "query_type",
            b"query_type",
        ],
    ) -> None: ...

global___WorkflowQuery = WorkflowQuery

class WorkflowQueryResult(google.protobuf.message.Message):
    """Answer to a `WorkflowQuery`"""

    DESCRIPTOR: google.protobuf.descriptor.Descriptor
    RESULT_TYPE_FIELD_NUMBER: builtins.int
    ANSWER_FIELD_NUMBER: builtins.int
    ERROR_MESSAGE_FIELD_NUMBER: builtins.int
    result_type: temporalio.api.enums.v1.query_pb2.QueryResultType.ValueType
    """Did the query succeed or fail?"""
    @property
    def answer(self) -> temporalio.api.common.v1.message_pb2.Payloads:
        """Set when the query succeeds with the results"""
        pass
    error_message: typing.Text
    """Mutually exclusive with `answer`. Set when the query fails."""
    def __init__(
        self,
        *,
        result_type: temporalio.api.enums.v1.query_pb2.QueryResultType.ValueType = ...,
        answer: typing.Optional[temporalio.api.common.v1.message_pb2.Payloads] = ...,
        error_message: typing.Text = ...,
    ) -> None: ...
    def HasField(
        self, field_name: typing_extensions.Literal["answer", b"answer"]
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "answer",
            b"answer",
            "error_message",
            b"error_message",
            "result_type",
            b"result_type",
        ],
    ) -> None: ...

global___WorkflowQueryResult = WorkflowQueryResult

class QueryRejected(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor
    STATUS_FIELD_NUMBER: builtins.int
    status: temporalio.api.enums.v1.workflow_pb2.WorkflowExecutionStatus.ValueType
    def __init__(
        self,
        *,
        status: temporalio.api.enums.v1.workflow_pb2.WorkflowExecutionStatus.ValueType = ...,
    ) -> None: ...
    def ClearField(
        self, field_name: typing_extensions.Literal["status", b"status"]
    ) -> None: ...

global___QueryRejected = QueryRejected
