"""
@generated by mypy-protobuf.  Do not edit manually!
isort:skip_file
"""

import builtins
import collections.abc
import sys

import google.protobuf.descriptor
import google.protobuf.internal.containers
import google.protobuf.message

import temporalio.api.common.v1.message_pb2
import temporalio.bridge.proto.activity_result.activity_result_pb2

if sys.version_info >= (3, 8):
    import typing as typing_extensions
else:
    import typing_extensions

DESCRIPTOR: google.protobuf.descriptor.FileDescriptor

class ActivityHeartbeat(google.protobuf.message.Message):
    """A request as given to `record_activity_heartbeat`"""

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    TASK_TOKEN_FIELD_NUMBER: builtins.int
    DETAILS_FIELD_NUMBER: builtins.int
    task_token: builtins.bytes
    @property
    def details(
        self,
    ) -> google.protobuf.internal.containers.RepeatedCompositeFieldContainer[
        temporalio.api.common.v1.message_pb2.Payload
    ]: ...
    def __init__(
        self,
        *,
        task_token: builtins.bytes = ...,
        details: collections.abc.Iterable[temporalio.api.common.v1.message_pb2.Payload]
        | None = ...,
    ) -> None: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "details", b"details", "task_token", b"task_token"
        ],
    ) -> None: ...

global___ActivityHeartbeat = ActivityHeartbeat

class ActivityTaskCompletion(google.protobuf.message.Message):
    """A request as given to `complete_activity_task`"""

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    TASK_TOKEN_FIELD_NUMBER: builtins.int
    RESULT_FIELD_NUMBER: builtins.int
    task_token: builtins.bytes
    @property
    def result(
        self,
    ) -> temporalio.bridge.proto.activity_result.activity_result_pb2.ActivityExecutionResult: ...
    def __init__(
        self,
        *,
        task_token: builtins.bytes = ...,
        result: temporalio.bridge.proto.activity_result.activity_result_pb2.ActivityExecutionResult
        | None = ...,
    ) -> None: ...
    def HasField(
        self, field_name: typing_extensions.Literal["result", b"result"]
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "result", b"result", "task_token", b"task_token"
        ],
    ) -> None: ...

global___ActivityTaskCompletion = ActivityTaskCompletion

class WorkflowSlotInfo(google.protobuf.message.Message):
    """Info about workflow task slot usage"""

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    WORKFLOW_TYPE_FIELD_NUMBER: builtins.int
    IS_STICKY_FIELD_NUMBER: builtins.int
    workflow_type: builtins.str
    is_sticky: builtins.bool
    def __init__(
        self,
        *,
        workflow_type: builtins.str = ...,
        is_sticky: builtins.bool = ...,
    ) -> None: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "is_sticky", b"is_sticky", "workflow_type", b"workflow_type"
        ],
    ) -> None: ...

global___WorkflowSlotInfo = WorkflowSlotInfo

class ActivitySlotInfo(google.protobuf.message.Message):
    """Info about activity task slot usage"""

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    ACTIVITY_TYPE_FIELD_NUMBER: builtins.int
    activity_type: builtins.str
    def __init__(
        self,
        *,
        activity_type: builtins.str = ...,
    ) -> None: ...
    def ClearField(
        self, field_name: typing_extensions.Literal["activity_type", b"activity_type"]
    ) -> None: ...

global___ActivitySlotInfo = ActivitySlotInfo

class LocalActivitySlotInfo(google.protobuf.message.Message):
    """Info about local activity slot usage"""

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    ACTIVITY_TYPE_FIELD_NUMBER: builtins.int
    activity_type: builtins.str
    def __init__(
        self,
        *,
        activity_type: builtins.str = ...,
    ) -> None: ...
    def ClearField(
        self, field_name: typing_extensions.Literal["activity_type", b"activity_type"]
    ) -> None: ...

global___LocalActivitySlotInfo = LocalActivitySlotInfo

class NexusSlotInfo(google.protobuf.message.Message):
    """Info about nexus task slot usage"""

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    SERVICE_FIELD_NUMBER: builtins.int
    OPERATION_FIELD_NUMBER: builtins.int
    service: builtins.str
    operation: builtins.str
    def __init__(
        self,
        *,
        service: builtins.str = ...,
        operation: builtins.str = ...,
    ) -> None: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "operation", b"operation", "service", b"service"
        ],
    ) -> None: ...

global___NexusSlotInfo = NexusSlotInfo
