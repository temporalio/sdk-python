"""
@generated by mypy-protobuf.  Do not edit manually!
isort:skip_file
*
Definitions of the different activity tasks returned from [crate::Core::poll_task].
"""

import builtins
import collections.abc
import sys
import typing

import google.protobuf.descriptor
import google.protobuf.duration_pb2
import google.protobuf.internal.containers
import google.protobuf.internal.enum_type_wrapper
import google.protobuf.message
import google.protobuf.timestamp_pb2

import temporalio.api.common.v1.message_pb2

if sys.version_info >= (3, 10):
    import typing as typing_extensions
else:
    import typing_extensions

DESCRIPTOR: google.protobuf.descriptor.FileDescriptor

class _ActivityCancelReason:
    ValueType = typing.NewType("ValueType", builtins.int)
    V: typing_extensions.TypeAlias = ValueType

class _ActivityCancelReasonEnumTypeWrapper(
    google.protobuf.internal.enum_type_wrapper._EnumTypeWrapper[
        _ActivityCancelReason.ValueType
    ],
    builtins.type,
):  # noqa: F821
    DESCRIPTOR: google.protobuf.descriptor.EnumDescriptor
    NOT_FOUND: _ActivityCancelReason.ValueType  # 0
    """The activity no longer exists according to server (may be already completed)"""
    CANCELLED: _ActivityCancelReason.ValueType  # 1
    """Activity was explicitly cancelled"""
    TIMED_OUT: _ActivityCancelReason.ValueType  # 2
    """Activity timed out"""
    WORKER_SHUTDOWN: _ActivityCancelReason.ValueType  # 3
    """Core is shutting down and the graceful timeout has elapsed"""

class ActivityCancelReason(
    _ActivityCancelReason, metaclass=_ActivityCancelReasonEnumTypeWrapper
): ...

NOT_FOUND: ActivityCancelReason.ValueType  # 0
"""The activity no longer exists according to server (may be already completed)"""
CANCELLED: ActivityCancelReason.ValueType  # 1
"""Activity was explicitly cancelled"""
TIMED_OUT: ActivityCancelReason.ValueType  # 2
"""Activity timed out"""
WORKER_SHUTDOWN: ActivityCancelReason.ValueType  # 3
"""Core is shutting down and the graceful timeout has elapsed"""
global___ActivityCancelReason = ActivityCancelReason

class ActivityTask(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    TASK_TOKEN_FIELD_NUMBER: builtins.int
    START_FIELD_NUMBER: builtins.int
    CANCEL_FIELD_NUMBER: builtins.int
    task_token: builtins.bytes
    """A unique identifier for this task"""
    @property
    def start(self) -> global___Start:
        """Start activity execution."""
    @property
    def cancel(self) -> global___Cancel:
        """Attempt to cancel activity execution."""
    def __init__(
        self,
        *,
        task_token: builtins.bytes = ...,
        start: global___Start | None = ...,
        cancel: global___Cancel | None = ...,
    ) -> None: ...
    def HasField(
        self,
        field_name: typing_extensions.Literal[
            "cancel", b"cancel", "start", b"start", "variant", b"variant"
        ],
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "cancel",
            b"cancel",
            "start",
            b"start",
            "task_token",
            b"task_token",
            "variant",
            b"variant",
        ],
    ) -> None: ...
    def WhichOneof(
        self, oneof_group: typing_extensions.Literal["variant", b"variant"]
    ) -> typing_extensions.Literal["start", "cancel"] | None: ...

global___ActivityTask = ActivityTask

class Start(google.protobuf.message.Message):
    """Begin executing an activity"""

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    class HeaderFieldsEntry(google.protobuf.message.Message):
        DESCRIPTOR: google.protobuf.descriptor.Descriptor

        KEY_FIELD_NUMBER: builtins.int
        VALUE_FIELD_NUMBER: builtins.int
        key: builtins.str
        @property
        def value(self) -> temporalio.api.common.v1.message_pb2.Payload: ...
        def __init__(
            self,
            *,
            key: builtins.str = ...,
            value: temporalio.api.common.v1.message_pb2.Payload | None = ...,
        ) -> None: ...
        def HasField(
            self, field_name: typing_extensions.Literal["value", b"value"]
        ) -> builtins.bool: ...
        def ClearField(
            self,
            field_name: typing_extensions.Literal["key", b"key", "value", b"value"],
        ) -> None: ...

    WORKFLOW_NAMESPACE_FIELD_NUMBER: builtins.int
    WORKFLOW_TYPE_FIELD_NUMBER: builtins.int
    WORKFLOW_EXECUTION_FIELD_NUMBER: builtins.int
    ACTIVITY_ID_FIELD_NUMBER: builtins.int
    ACTIVITY_TYPE_FIELD_NUMBER: builtins.int
    HEADER_FIELDS_FIELD_NUMBER: builtins.int
    INPUT_FIELD_NUMBER: builtins.int
    HEARTBEAT_DETAILS_FIELD_NUMBER: builtins.int
    SCHEDULED_TIME_FIELD_NUMBER: builtins.int
    CURRENT_ATTEMPT_SCHEDULED_TIME_FIELD_NUMBER: builtins.int
    STARTED_TIME_FIELD_NUMBER: builtins.int
    ATTEMPT_FIELD_NUMBER: builtins.int
    SCHEDULE_TO_CLOSE_TIMEOUT_FIELD_NUMBER: builtins.int
    START_TO_CLOSE_TIMEOUT_FIELD_NUMBER: builtins.int
    HEARTBEAT_TIMEOUT_FIELD_NUMBER: builtins.int
    RETRY_POLICY_FIELD_NUMBER: builtins.int
    PRIORITY_FIELD_NUMBER: builtins.int
    IS_LOCAL_FIELD_NUMBER: builtins.int
    workflow_namespace: builtins.str
    """The namespace the workflow lives in"""
    workflow_type: builtins.str
    """The workflow's type name or function identifier"""
    @property
    def workflow_execution(
        self,
    ) -> temporalio.api.common.v1.message_pb2.WorkflowExecution:
        """The workflow execution which requested this activity"""
    activity_id: builtins.str
    """The activity's ID"""
    activity_type: builtins.str
    """The activity's type name or function identifier"""
    @property
    def header_fields(
        self,
    ) -> google.protobuf.internal.containers.MessageMap[
        builtins.str, temporalio.api.common.v1.message_pb2.Payload
    ]: ...
    @property
    def input(
        self,
    ) -> google.protobuf.internal.containers.RepeatedCompositeFieldContainer[
        temporalio.api.common.v1.message_pb2.Payload
    ]:
        """Arguments to the activity"""
    @property
    def heartbeat_details(
        self,
    ) -> google.protobuf.internal.containers.RepeatedCompositeFieldContainer[
        temporalio.api.common.v1.message_pb2.Payload
    ]:
        """The last details that were recorded by a heartbeat when this task was generated"""
    @property
    def scheduled_time(self) -> google.protobuf.timestamp_pb2.Timestamp:
        """When the task was *first* scheduled"""
    @property
    def current_attempt_scheduled_time(self) -> google.protobuf.timestamp_pb2.Timestamp:
        """When this current attempt at the task was scheduled"""
    @property
    def started_time(self) -> google.protobuf.timestamp_pb2.Timestamp:
        """When this attempt was started, which is to say when core received it by polling."""
    attempt: builtins.int
    @property
    def schedule_to_close_timeout(self) -> google.protobuf.duration_pb2.Duration:
        """Timeout from the first schedule time to completion"""
    @property
    def start_to_close_timeout(self) -> google.protobuf.duration_pb2.Duration:
        """Timeout from starting an attempt to reporting its result"""
    @property
    def heartbeat_timeout(self) -> google.protobuf.duration_pb2.Duration:
        """If set a heartbeat must be reported within this interval"""
    @property
    def retry_policy(self) -> temporalio.api.common.v1.message_pb2.RetryPolicy:
        """This is an actual retry policy the service uses. It can be different from the one provided
        (or not) during activity scheduling as the service can override the provided one in case its
        values are not specified or exceed configured system limits.
        """
    @property
    def priority(self) -> temporalio.api.common.v1.message_pb2.Priority:
        """Priority of this activity. Local activities will always have this field set to the default."""
    is_local: builtins.bool
    """Set to true if this is a local activity. Note that heartbeating does not apply to local
    activities.
    """
    def __init__(
        self,
        *,
        workflow_namespace: builtins.str = ...,
        workflow_type: builtins.str = ...,
        workflow_execution: temporalio.api.common.v1.message_pb2.WorkflowExecution
        | None = ...,
        activity_id: builtins.str = ...,
        activity_type: builtins.str = ...,
        header_fields: collections.abc.Mapping[
            builtins.str, temporalio.api.common.v1.message_pb2.Payload
        ]
        | None = ...,
        input: collections.abc.Iterable[temporalio.api.common.v1.message_pb2.Payload]
        | None = ...,
        heartbeat_details: collections.abc.Iterable[
            temporalio.api.common.v1.message_pb2.Payload
        ]
        | None = ...,
        scheduled_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
        current_attempt_scheduled_time: google.protobuf.timestamp_pb2.Timestamp
        | None = ...,
        started_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
        attempt: builtins.int = ...,
        schedule_to_close_timeout: google.protobuf.duration_pb2.Duration | None = ...,
        start_to_close_timeout: google.protobuf.duration_pb2.Duration | None = ...,
        heartbeat_timeout: google.protobuf.duration_pb2.Duration | None = ...,
        retry_policy: temporalio.api.common.v1.message_pb2.RetryPolicy | None = ...,
        priority: temporalio.api.common.v1.message_pb2.Priority | None = ...,
        is_local: builtins.bool = ...,
    ) -> None: ...
    def HasField(
        self,
        field_name: typing_extensions.Literal[
            "current_attempt_scheduled_time",
            b"current_attempt_scheduled_time",
            "heartbeat_timeout",
            b"heartbeat_timeout",
            "priority",
            b"priority",
            "retry_policy",
            b"retry_policy",
            "schedule_to_close_timeout",
            b"schedule_to_close_timeout",
            "scheduled_time",
            b"scheduled_time",
            "start_to_close_timeout",
            b"start_to_close_timeout",
            "started_time",
            b"started_time",
            "workflow_execution",
            b"workflow_execution",
        ],
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "activity_id",
            b"activity_id",
            "activity_type",
            b"activity_type",
            "attempt",
            b"attempt",
            "current_attempt_scheduled_time",
            b"current_attempt_scheduled_time",
            "header_fields",
            b"header_fields",
            "heartbeat_details",
            b"heartbeat_details",
            "heartbeat_timeout",
            b"heartbeat_timeout",
            "input",
            b"input",
            "is_local",
            b"is_local",
            "priority",
            b"priority",
            "retry_policy",
            b"retry_policy",
            "schedule_to_close_timeout",
            b"schedule_to_close_timeout",
            "scheduled_time",
            b"scheduled_time",
            "start_to_close_timeout",
            b"start_to_close_timeout",
            "started_time",
            b"started_time",
            "workflow_execution",
            b"workflow_execution",
            "workflow_namespace",
            b"workflow_namespace",
            "workflow_type",
            b"workflow_type",
        ],
    ) -> None: ...

global___Start = Start

class Cancel(google.protobuf.message.Message):
    """Attempt to cancel a running activity"""

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    REASON_FIELD_NUMBER: builtins.int
    reason: global___ActivityCancelReason.ValueType
    def __init__(
        self,
        *,
        reason: global___ActivityCancelReason.ValueType = ...,
    ) -> None: ...
    def ClearField(
        self, field_name: typing_extensions.Literal["reason", b"reason"]
    ) -> None: ...

global___Cancel = Cancel
