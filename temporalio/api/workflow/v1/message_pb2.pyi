"""
@generated by mypy-protobuf.  Do not edit manually!
isort:skip_file
The MIT License

Copyright (c) 2020 Temporal Technologies Inc.  All rights reserved.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""
import builtins
import collections.abc
import google.protobuf.descriptor
import google.protobuf.duration_pb2
import google.protobuf.internal.containers
import google.protobuf.message
import google.protobuf.timestamp_pb2
import sys
import temporalio.api.common.v1.message_pb2
import temporalio.api.enums.v1.workflow_pb2
import temporalio.api.failure.v1.message_pb2
import temporalio.api.taskqueue.v1.message_pb2

if sys.version_info >= (3, 8):
    import typing as typing_extensions
else:
    import typing_extensions

DESCRIPTOR: google.protobuf.descriptor.FileDescriptor

class WorkflowExecutionInfo(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    EXECUTION_FIELD_NUMBER: builtins.int
    TYPE_FIELD_NUMBER: builtins.int
    START_TIME_FIELD_NUMBER: builtins.int
    CLOSE_TIME_FIELD_NUMBER: builtins.int
    STATUS_FIELD_NUMBER: builtins.int
    HISTORY_LENGTH_FIELD_NUMBER: builtins.int
    PARENT_NAMESPACE_ID_FIELD_NUMBER: builtins.int
    PARENT_EXECUTION_FIELD_NUMBER: builtins.int
    EXECUTION_TIME_FIELD_NUMBER: builtins.int
    MEMO_FIELD_NUMBER: builtins.int
    SEARCH_ATTRIBUTES_FIELD_NUMBER: builtins.int
    AUTO_RESET_POINTS_FIELD_NUMBER: builtins.int
    TASK_QUEUE_FIELD_NUMBER: builtins.int
    STATE_TRANSITION_COUNT_FIELD_NUMBER: builtins.int
    HISTORY_SIZE_BYTES_FIELD_NUMBER: builtins.int
    MOST_RECENT_WORKER_VERSION_STAMP_FIELD_NUMBER: builtins.int
    @property
    def execution(self) -> temporalio.api.common.v1.message_pb2.WorkflowExecution: ...
    @property
    def type(self) -> temporalio.api.common.v1.message_pb2.WorkflowType: ...
    @property
    def start_time(self) -> google.protobuf.timestamp_pb2.Timestamp: ...
    @property
    def close_time(self) -> google.protobuf.timestamp_pb2.Timestamp: ...
    status: temporalio.api.enums.v1.workflow_pb2.WorkflowExecutionStatus.ValueType
    history_length: builtins.int
    parent_namespace_id: builtins.str
    @property
    def parent_execution(
        self,
    ) -> temporalio.api.common.v1.message_pb2.WorkflowExecution: ...
    @property
    def execution_time(self) -> google.protobuf.timestamp_pb2.Timestamp: ...
    @property
    def memo(self) -> temporalio.api.common.v1.message_pb2.Memo: ...
    @property
    def search_attributes(
        self,
    ) -> temporalio.api.common.v1.message_pb2.SearchAttributes: ...
    @property
    def auto_reset_points(self) -> global___ResetPoints: ...
    task_queue: builtins.str
    state_transition_count: builtins.int
    history_size_bytes: builtins.int
    @property
    def most_recent_worker_version_stamp(
        self,
    ) -> temporalio.api.common.v1.message_pb2.WorkerVersionStamp:
        """If set, the most recent worker version stamp that appeared in a workflow task completion"""
    def __init__(
        self,
        *,
        execution: temporalio.api.common.v1.message_pb2.WorkflowExecution | None = ...,
        type: temporalio.api.common.v1.message_pb2.WorkflowType | None = ...,
        start_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
        close_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
        status: temporalio.api.enums.v1.workflow_pb2.WorkflowExecutionStatus.ValueType = ...,
        history_length: builtins.int = ...,
        parent_namespace_id: builtins.str = ...,
        parent_execution: temporalio.api.common.v1.message_pb2.WorkflowExecution
        | None = ...,
        execution_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
        memo: temporalio.api.common.v1.message_pb2.Memo | None = ...,
        search_attributes: temporalio.api.common.v1.message_pb2.SearchAttributes
        | None = ...,
        auto_reset_points: global___ResetPoints | None = ...,
        task_queue: builtins.str = ...,
        state_transition_count: builtins.int = ...,
        history_size_bytes: builtins.int = ...,
        most_recent_worker_version_stamp: temporalio.api.common.v1.message_pb2.WorkerVersionStamp
        | None = ...,
    ) -> None: ...
    def HasField(
        self,
        field_name: typing_extensions.Literal[
            "auto_reset_points",
            b"auto_reset_points",
            "close_time",
            b"close_time",
            "execution",
            b"execution",
            "execution_time",
            b"execution_time",
            "memo",
            b"memo",
            "most_recent_worker_version_stamp",
            b"most_recent_worker_version_stamp",
            "parent_execution",
            b"parent_execution",
            "search_attributes",
            b"search_attributes",
            "start_time",
            b"start_time",
            "type",
            b"type",
        ],
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "auto_reset_points",
            b"auto_reset_points",
            "close_time",
            b"close_time",
            "execution",
            b"execution",
            "execution_time",
            b"execution_time",
            "history_length",
            b"history_length",
            "history_size_bytes",
            b"history_size_bytes",
            "memo",
            b"memo",
            "most_recent_worker_version_stamp",
            b"most_recent_worker_version_stamp",
            "parent_execution",
            b"parent_execution",
            "parent_namespace_id",
            b"parent_namespace_id",
            "search_attributes",
            b"search_attributes",
            "start_time",
            b"start_time",
            "state_transition_count",
            b"state_transition_count",
            "status",
            b"status",
            "task_queue",
            b"task_queue",
            "type",
            b"type",
        ],
    ) -> None: ...

global___WorkflowExecutionInfo = WorkflowExecutionInfo

class WorkflowExecutionConfig(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    TASK_QUEUE_FIELD_NUMBER: builtins.int
    WORKFLOW_EXECUTION_TIMEOUT_FIELD_NUMBER: builtins.int
    WORKFLOW_RUN_TIMEOUT_FIELD_NUMBER: builtins.int
    DEFAULT_WORKFLOW_TASK_TIMEOUT_FIELD_NUMBER: builtins.int
    @property
    def task_queue(self) -> temporalio.api.taskqueue.v1.message_pb2.TaskQueue: ...
    @property
    def workflow_execution_timeout(self) -> google.protobuf.duration_pb2.Duration: ...
    @property
    def workflow_run_timeout(self) -> google.protobuf.duration_pb2.Duration: ...
    @property
    def default_workflow_task_timeout(
        self,
    ) -> google.protobuf.duration_pb2.Duration: ...
    def __init__(
        self,
        *,
        task_queue: temporalio.api.taskqueue.v1.message_pb2.TaskQueue | None = ...,
        workflow_execution_timeout: google.protobuf.duration_pb2.Duration | None = ...,
        workflow_run_timeout: google.protobuf.duration_pb2.Duration | None = ...,
        default_workflow_task_timeout: google.protobuf.duration_pb2.Duration
        | None = ...,
    ) -> None: ...
    def HasField(
        self,
        field_name: typing_extensions.Literal[
            "default_workflow_task_timeout",
            b"default_workflow_task_timeout",
            "task_queue",
            b"task_queue",
            "workflow_execution_timeout",
            b"workflow_execution_timeout",
            "workflow_run_timeout",
            b"workflow_run_timeout",
        ],
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "default_workflow_task_timeout",
            b"default_workflow_task_timeout",
            "task_queue",
            b"task_queue",
            "workflow_execution_timeout",
            b"workflow_execution_timeout",
            "workflow_run_timeout",
            b"workflow_run_timeout",
        ],
    ) -> None: ...

global___WorkflowExecutionConfig = WorkflowExecutionConfig

class PendingActivityInfo(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    ACTIVITY_ID_FIELD_NUMBER: builtins.int
    ACTIVITY_TYPE_FIELD_NUMBER: builtins.int
    STATE_FIELD_NUMBER: builtins.int
    HEARTBEAT_DETAILS_FIELD_NUMBER: builtins.int
    LAST_HEARTBEAT_TIME_FIELD_NUMBER: builtins.int
    LAST_STARTED_TIME_FIELD_NUMBER: builtins.int
    ATTEMPT_FIELD_NUMBER: builtins.int
    MAXIMUM_ATTEMPTS_FIELD_NUMBER: builtins.int
    SCHEDULED_TIME_FIELD_NUMBER: builtins.int
    EXPIRATION_TIME_FIELD_NUMBER: builtins.int
    LAST_FAILURE_FIELD_NUMBER: builtins.int
    LAST_WORKER_IDENTITY_FIELD_NUMBER: builtins.int
    activity_id: builtins.str
    @property
    def activity_type(self) -> temporalio.api.common.v1.message_pb2.ActivityType: ...
    state: temporalio.api.enums.v1.workflow_pb2.PendingActivityState.ValueType
    @property
    def heartbeat_details(self) -> temporalio.api.common.v1.message_pb2.Payloads: ...
    @property
    def last_heartbeat_time(self) -> google.protobuf.timestamp_pb2.Timestamp: ...
    @property
    def last_started_time(self) -> google.protobuf.timestamp_pb2.Timestamp: ...
    attempt: builtins.int
    maximum_attempts: builtins.int
    @property
    def scheduled_time(self) -> google.protobuf.timestamp_pb2.Timestamp: ...
    @property
    def expiration_time(self) -> google.protobuf.timestamp_pb2.Timestamp: ...
    @property
    def last_failure(self) -> temporalio.api.failure.v1.message_pb2.Failure: ...
    last_worker_identity: builtins.str
    def __init__(
        self,
        *,
        activity_id: builtins.str = ...,
        activity_type: temporalio.api.common.v1.message_pb2.ActivityType | None = ...,
        state: temporalio.api.enums.v1.workflow_pb2.PendingActivityState.ValueType = ...,
        heartbeat_details: temporalio.api.common.v1.message_pb2.Payloads | None = ...,
        last_heartbeat_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
        last_started_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
        attempt: builtins.int = ...,
        maximum_attempts: builtins.int = ...,
        scheduled_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
        expiration_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
        last_failure: temporalio.api.failure.v1.message_pb2.Failure | None = ...,
        last_worker_identity: builtins.str = ...,
    ) -> None: ...
    def HasField(
        self,
        field_name: typing_extensions.Literal[
            "activity_type",
            b"activity_type",
            "expiration_time",
            b"expiration_time",
            "heartbeat_details",
            b"heartbeat_details",
            "last_failure",
            b"last_failure",
            "last_heartbeat_time",
            b"last_heartbeat_time",
            "last_started_time",
            b"last_started_time",
            "scheduled_time",
            b"scheduled_time",
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
            "expiration_time",
            b"expiration_time",
            "heartbeat_details",
            b"heartbeat_details",
            "last_failure",
            b"last_failure",
            "last_heartbeat_time",
            b"last_heartbeat_time",
            "last_started_time",
            b"last_started_time",
            "last_worker_identity",
            b"last_worker_identity",
            "maximum_attempts",
            b"maximum_attempts",
            "scheduled_time",
            b"scheduled_time",
            "state",
            b"state",
        ],
    ) -> None: ...

global___PendingActivityInfo = PendingActivityInfo

class PendingChildExecutionInfo(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    WORKFLOW_ID_FIELD_NUMBER: builtins.int
    RUN_ID_FIELD_NUMBER: builtins.int
    WORKFLOW_TYPE_NAME_FIELD_NUMBER: builtins.int
    INITIATED_ID_FIELD_NUMBER: builtins.int
    PARENT_CLOSE_POLICY_FIELD_NUMBER: builtins.int
    workflow_id: builtins.str
    run_id: builtins.str
    workflow_type_name: builtins.str
    initiated_id: builtins.int
    parent_close_policy: temporalio.api.enums.v1.workflow_pb2.ParentClosePolicy.ValueType
    """Default: PARENT_CLOSE_POLICY_TERMINATE."""
    def __init__(
        self,
        *,
        workflow_id: builtins.str = ...,
        run_id: builtins.str = ...,
        workflow_type_name: builtins.str = ...,
        initiated_id: builtins.int = ...,
        parent_close_policy: temporalio.api.enums.v1.workflow_pb2.ParentClosePolicy.ValueType = ...,
    ) -> None: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "initiated_id",
            b"initiated_id",
            "parent_close_policy",
            b"parent_close_policy",
            "run_id",
            b"run_id",
            "workflow_id",
            b"workflow_id",
            "workflow_type_name",
            b"workflow_type_name",
        ],
    ) -> None: ...

global___PendingChildExecutionInfo = PendingChildExecutionInfo

class PendingWorkflowTaskInfo(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    STATE_FIELD_NUMBER: builtins.int
    SCHEDULED_TIME_FIELD_NUMBER: builtins.int
    ORIGINAL_SCHEDULED_TIME_FIELD_NUMBER: builtins.int
    STARTED_TIME_FIELD_NUMBER: builtins.int
    ATTEMPT_FIELD_NUMBER: builtins.int
    state: temporalio.api.enums.v1.workflow_pb2.PendingWorkflowTaskState.ValueType
    @property
    def scheduled_time(self) -> google.protobuf.timestamp_pb2.Timestamp: ...
    @property
    def original_scheduled_time(self) -> google.protobuf.timestamp_pb2.Timestamp:
        """original_scheduled_time is the scheduled time of the first workflow task during workflow task heartbeat.
        Heartbeat workflow task is done by RespondWorkflowTaskComplete with ForceCreateNewWorkflowTask == true and no command
        In this case, OriginalScheduledTime won't change. Then when current time - original_scheduled_time exceeds
        some threshold, the workflow task will be forced timeout.
        """
    @property
    def started_time(self) -> google.protobuf.timestamp_pb2.Timestamp: ...
    attempt: builtins.int
    def __init__(
        self,
        *,
        state: temporalio.api.enums.v1.workflow_pb2.PendingWorkflowTaskState.ValueType = ...,
        scheduled_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
        original_scheduled_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
        started_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
        attempt: builtins.int = ...,
    ) -> None: ...
    def HasField(
        self,
        field_name: typing_extensions.Literal[
            "original_scheduled_time",
            b"original_scheduled_time",
            "scheduled_time",
            b"scheduled_time",
            "started_time",
            b"started_time",
        ],
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "attempt",
            b"attempt",
            "original_scheduled_time",
            b"original_scheduled_time",
            "scheduled_time",
            b"scheduled_time",
            "started_time",
            b"started_time",
            "state",
            b"state",
        ],
    ) -> None: ...

global___PendingWorkflowTaskInfo = PendingWorkflowTaskInfo

class ResetPoints(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    POINTS_FIELD_NUMBER: builtins.int
    @property
    def points(
        self,
    ) -> google.protobuf.internal.containers.RepeatedCompositeFieldContainer[
        global___ResetPointInfo
    ]: ...
    def __init__(
        self,
        *,
        points: collections.abc.Iterable[global___ResetPointInfo] | None = ...,
    ) -> None: ...
    def ClearField(
        self, field_name: typing_extensions.Literal["points", b"points"]
    ) -> None: ...

global___ResetPoints = ResetPoints

class ResetPointInfo(google.protobuf.message.Message):
    """ResetPointInfo records the workflow event id that is the first one processed by a given
    build id or binary checksum. A new reset point will be created if either build id or binary
    checksum changes (although in general only one or the other will be used at a time).
    """

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    BUILD_ID_FIELD_NUMBER: builtins.int
    BINARY_CHECKSUM_FIELD_NUMBER: builtins.int
    RUN_ID_FIELD_NUMBER: builtins.int
    FIRST_WORKFLOW_TASK_COMPLETED_ID_FIELD_NUMBER: builtins.int
    CREATE_TIME_FIELD_NUMBER: builtins.int
    EXPIRE_TIME_FIELD_NUMBER: builtins.int
    RESETTABLE_FIELD_NUMBER: builtins.int
    build_id: builtins.str
    """Worker build id."""
    binary_checksum: builtins.str
    """A worker binary version identifier (deprecated)."""
    run_id: builtins.str
    """The first run ID in the execution chain that was touched by this worker build."""
    first_workflow_task_completed_id: builtins.int
    """Event ID of the first WorkflowTaskCompleted event processed by this worker build."""
    @property
    def create_time(self) -> google.protobuf.timestamp_pb2.Timestamp: ...
    @property
    def expire_time(self) -> google.protobuf.timestamp_pb2.Timestamp:
        """(-- api-linter: core::0214::resource-expiry=disabled
            aip.dev/not-precedent: TTL is not defined for ResetPointInfo. --)
        The time that the run is deleted due to retention.
        """
    resettable: builtins.bool
    """false if the reset point has pending childWFs/reqCancels/signalExternals."""
    def __init__(
        self,
        *,
        build_id: builtins.str = ...,
        binary_checksum: builtins.str = ...,
        run_id: builtins.str = ...,
        first_workflow_task_completed_id: builtins.int = ...,
        create_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
        expire_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
        resettable: builtins.bool = ...,
    ) -> None: ...
    def HasField(
        self,
        field_name: typing_extensions.Literal[
            "create_time", b"create_time", "expire_time", b"expire_time"
        ],
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "binary_checksum",
            b"binary_checksum",
            "build_id",
            b"build_id",
            "create_time",
            b"create_time",
            "expire_time",
            b"expire_time",
            "first_workflow_task_completed_id",
            b"first_workflow_task_completed_id",
            "resettable",
            b"resettable",
            "run_id",
            b"run_id",
        ],
    ) -> None: ...

global___ResetPointInfo = ResetPointInfo

class NewWorkflowExecutionInfo(google.protobuf.message.Message):
    """NewWorkflowExecutionInfo is a shared message that encapsulates all the
    required arguments to starting a workflow in different contexts.
    """

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    WORKFLOW_ID_FIELD_NUMBER: builtins.int
    WORKFLOW_TYPE_FIELD_NUMBER: builtins.int
    TASK_QUEUE_FIELD_NUMBER: builtins.int
    INPUT_FIELD_NUMBER: builtins.int
    WORKFLOW_EXECUTION_TIMEOUT_FIELD_NUMBER: builtins.int
    WORKFLOW_RUN_TIMEOUT_FIELD_NUMBER: builtins.int
    WORKFLOW_TASK_TIMEOUT_FIELD_NUMBER: builtins.int
    WORKFLOW_ID_REUSE_POLICY_FIELD_NUMBER: builtins.int
    RETRY_POLICY_FIELD_NUMBER: builtins.int
    CRON_SCHEDULE_FIELD_NUMBER: builtins.int
    MEMO_FIELD_NUMBER: builtins.int
    SEARCH_ATTRIBUTES_FIELD_NUMBER: builtins.int
    HEADER_FIELD_NUMBER: builtins.int
    workflow_id: builtins.str
    @property
    def workflow_type(self) -> temporalio.api.common.v1.message_pb2.WorkflowType: ...
    @property
    def task_queue(self) -> temporalio.api.taskqueue.v1.message_pb2.TaskQueue: ...
    @property
    def input(self) -> temporalio.api.common.v1.message_pb2.Payloads:
        """Serialized arguments to the workflow."""
    @property
    def workflow_execution_timeout(self) -> google.protobuf.duration_pb2.Duration:
        """Total workflow execution timeout including retries and continue as new."""
    @property
    def workflow_run_timeout(self) -> google.protobuf.duration_pb2.Duration:
        """Timeout of a single workflow run."""
    @property
    def workflow_task_timeout(self) -> google.protobuf.duration_pb2.Duration:
        """Timeout of a single workflow task."""
    workflow_id_reuse_policy: temporalio.api.enums.v1.workflow_pb2.WorkflowIdReusePolicy.ValueType
    """Default: WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE."""
    @property
    def retry_policy(self) -> temporalio.api.common.v1.message_pb2.RetryPolicy:
        """The retry policy for the workflow. Will never exceed `workflow_execution_timeout`."""
    cron_schedule: builtins.str
    """See https://docs.temporal.io/docs/content/what-is-a-temporal-cron-job/"""
    @property
    def memo(self) -> temporalio.api.common.v1.message_pb2.Memo: ...
    @property
    def search_attributes(
        self,
    ) -> temporalio.api.common.v1.message_pb2.SearchAttributes: ...
    @property
    def header(self) -> temporalio.api.common.v1.message_pb2.Header: ...
    def __init__(
        self,
        *,
        workflow_id: builtins.str = ...,
        workflow_type: temporalio.api.common.v1.message_pb2.WorkflowType | None = ...,
        task_queue: temporalio.api.taskqueue.v1.message_pb2.TaskQueue | None = ...,
        input: temporalio.api.common.v1.message_pb2.Payloads | None = ...,
        workflow_execution_timeout: google.protobuf.duration_pb2.Duration | None = ...,
        workflow_run_timeout: google.protobuf.duration_pb2.Duration | None = ...,
        workflow_task_timeout: google.protobuf.duration_pb2.Duration | None = ...,
        workflow_id_reuse_policy: temporalio.api.enums.v1.workflow_pb2.WorkflowIdReusePolicy.ValueType = ...,
        retry_policy: temporalio.api.common.v1.message_pb2.RetryPolicy | None = ...,
        cron_schedule: builtins.str = ...,
        memo: temporalio.api.common.v1.message_pb2.Memo | None = ...,
        search_attributes: temporalio.api.common.v1.message_pb2.SearchAttributes
        | None = ...,
        header: temporalio.api.common.v1.message_pb2.Header | None = ...,
    ) -> None: ...
    def HasField(
        self,
        field_name: typing_extensions.Literal[
            "header",
            b"header",
            "input",
            b"input",
            "memo",
            b"memo",
            "retry_policy",
            b"retry_policy",
            "search_attributes",
            b"search_attributes",
            "task_queue",
            b"task_queue",
            "workflow_execution_timeout",
            b"workflow_execution_timeout",
            "workflow_run_timeout",
            b"workflow_run_timeout",
            "workflow_task_timeout",
            b"workflow_task_timeout",
            "workflow_type",
            b"workflow_type",
        ],
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "cron_schedule",
            b"cron_schedule",
            "header",
            b"header",
            "input",
            b"input",
            "memo",
            b"memo",
            "retry_policy",
            b"retry_policy",
            "search_attributes",
            b"search_attributes",
            "task_queue",
            b"task_queue",
            "workflow_execution_timeout",
            b"workflow_execution_timeout",
            "workflow_id",
            b"workflow_id",
            "workflow_id_reuse_policy",
            b"workflow_id_reuse_policy",
            "workflow_run_timeout",
            b"workflow_run_timeout",
            "workflow_task_timeout",
            b"workflow_task_timeout",
            "workflow_type",
            b"workflow_type",
        ],
    ) -> None: ...

global___NewWorkflowExecutionInfo = NewWorkflowExecutionInfo
