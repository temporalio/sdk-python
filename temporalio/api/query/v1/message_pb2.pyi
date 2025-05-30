"""
@generated by mypy-protobuf.  Do not edit manually!
isort:skip_file
"""

import builtins
import sys

import google.protobuf.descriptor
import google.protobuf.message

import temporalio.api.common.v1.message_pb2
import temporalio.api.enums.v1.query_pb2
import temporalio.api.enums.v1.workflow_pb2
import temporalio.api.failure.v1.message_pb2

if sys.version_info >= (3, 8):
    import typing as typing_extensions
else:
    import typing_extensions

DESCRIPTOR: google.protobuf.descriptor.FileDescriptor

class WorkflowQuery(google.protobuf.message.Message):
    """See https://docs.temporal.io/docs/concepts/queries/"""

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    QUERY_TYPE_FIELD_NUMBER: builtins.int
    QUERY_ARGS_FIELD_NUMBER: builtins.int
    HEADER_FIELD_NUMBER: builtins.int
    query_type: builtins.str
    """The workflow-author-defined identifier of the query. Typically a function name."""
    @property
    def query_args(self) -> temporalio.api.common.v1.message_pb2.Payloads:
        """Serialized arguments that will be provided to the query handler."""
    @property
    def header(self) -> temporalio.api.common.v1.message_pb2.Header:
        """Headers that were passed by the caller of the query and copied by temporal
        server into the workflow task.
        """
    def __init__(
        self,
        *,
        query_type: builtins.str = ...,
        query_args: temporalio.api.common.v1.message_pb2.Payloads | None = ...,
        header: temporalio.api.common.v1.message_pb2.Header | None = ...,
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
    FAILURE_FIELD_NUMBER: builtins.int
    result_type: temporalio.api.enums.v1.query_pb2.QueryResultType.ValueType
    """Did the query succeed or fail?"""
    @property
    def answer(self) -> temporalio.api.common.v1.message_pb2.Payloads:
        """Set when the query succeeds with the results.
        Mutually exclusive with `error_message` and `failure`.
        """
    error_message: builtins.str
    """Mutually exclusive with `answer`. Set when the query fails.
    See also the newer `failure` field.
    """
    @property
    def failure(self) -> temporalio.api.failure.v1.message_pb2.Failure:
        """The full reason for this query failure. This field is newer than `error_message` and can be encoded by the SDK's
        failure converter to support E2E encryption of messages and stack traces.
        Mutually exclusive with `answer`. Set when the query fails.
        """
    def __init__(
        self,
        *,
        result_type: temporalio.api.enums.v1.query_pb2.QueryResultType.ValueType = ...,
        answer: temporalio.api.common.v1.message_pb2.Payloads | None = ...,
        error_message: builtins.str = ...,
        failure: temporalio.api.failure.v1.message_pb2.Failure | None = ...,
    ) -> None: ...
    def HasField(
        self,
        field_name: typing_extensions.Literal[
            "answer", b"answer", "failure", b"failure"
        ],
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "answer",
            b"answer",
            "error_message",
            b"error_message",
            "failure",
            b"failure",
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
