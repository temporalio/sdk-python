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
import sys

import google.protobuf.descriptor
import google.protobuf.duration_pb2
import google.protobuf.internal.containers
import google.protobuf.message
import google.protobuf.timestamp_pb2
import google.protobuf.wrappers_pb2

import temporalio.api.common.v1.message_pb2
import temporalio.api.enums.v1.task_queue_pb2

if sys.version_info >= (3, 8):
    import typing as typing_extensions
else:
    import typing_extensions

DESCRIPTOR: google.protobuf.descriptor.FileDescriptor

class TaskQueue(google.protobuf.message.Message):
    """See https://docs.temporal.io/docs/concepts/task-queues/"""

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    NAME_FIELD_NUMBER: builtins.int
    KIND_FIELD_NUMBER: builtins.int
    NORMAL_NAME_FIELD_NUMBER: builtins.int
    name: builtins.str
    kind: temporalio.api.enums.v1.task_queue_pb2.TaskQueueKind.ValueType
    """Default: TASK_QUEUE_KIND_NORMAL."""
    normal_name: builtins.str
    """Iff kind == TASK_QUEUE_KIND_STICKY, then this field contains the name of
    the normal task queue that the sticky worker is running on.
    """
    def __init__(
        self,
        *,
        name: builtins.str = ...,
        kind: temporalio.api.enums.v1.task_queue_pb2.TaskQueueKind.ValueType = ...,
        normal_name: builtins.str = ...,
    ) -> None: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "kind", b"kind", "name", b"name", "normal_name", b"normal_name"
        ],
    ) -> None: ...

global___TaskQueue = TaskQueue

class TaskQueueMetadata(google.protobuf.message.Message):
    """Only applies to activity task queues"""

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    MAX_TASKS_PER_SECOND_FIELD_NUMBER: builtins.int
    @property
    def max_tasks_per_second(self) -> google.protobuf.wrappers_pb2.DoubleValue:
        """Allows throttling dispatch of tasks from this queue"""
    def __init__(
        self,
        *,
        max_tasks_per_second: google.protobuf.wrappers_pb2.DoubleValue | None = ...,
    ) -> None: ...
    def HasField(
        self,
        field_name: typing_extensions.Literal[
            "max_tasks_per_second", b"max_tasks_per_second"
        ],
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "max_tasks_per_second", b"max_tasks_per_second"
        ],
    ) -> None: ...

global___TaskQueueMetadata = TaskQueueMetadata

class TaskQueueVersionSelection(google.protobuf.message.Message):
    """Used for specifying versions the caller is interested in."""

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    BUILD_IDS_FIELD_NUMBER: builtins.int
    UNVERSIONED_FIELD_NUMBER: builtins.int
    ALL_ACTIVE_FIELD_NUMBER: builtins.int
    @property
    def build_ids(
        self,
    ) -> google.protobuf.internal.containers.RepeatedScalarFieldContainer[builtins.str]:
        """Include specific Build IDs."""
    unversioned: builtins.bool
    """Include the unversioned queue."""
    all_active: builtins.bool
    """Include all active versions. A version is considered active if it has had new
    tasks or polls recently.
    """
    def __init__(
        self,
        *,
        build_ids: collections.abc.Iterable[builtins.str] | None = ...,
        unversioned: builtins.bool = ...,
        all_active: builtins.bool = ...,
    ) -> None: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "all_active",
            b"all_active",
            "build_ids",
            b"build_ids",
            "unversioned",
            b"unversioned",
        ],
    ) -> None: ...

global___TaskQueueVersionSelection = TaskQueueVersionSelection

class TaskQueueVersionInfo(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    class TypesInfoEntry(google.protobuf.message.Message):
        DESCRIPTOR: google.protobuf.descriptor.Descriptor

        KEY_FIELD_NUMBER: builtins.int
        VALUE_FIELD_NUMBER: builtins.int
        key: builtins.int
        @property
        def value(self) -> global___TaskQueueTypeInfo: ...
        def __init__(
            self,
            *,
            key: builtins.int = ...,
            value: global___TaskQueueTypeInfo | None = ...,
        ) -> None: ...
        def HasField(
            self, field_name: typing_extensions.Literal["value", b"value"]
        ) -> builtins.bool: ...
        def ClearField(
            self,
            field_name: typing_extensions.Literal["key", b"key", "value", b"value"],
        ) -> None: ...

    TYPES_INFO_FIELD_NUMBER: builtins.int
    TASK_REACHABILITY_FIELD_NUMBER: builtins.int
    @property
    def types_info(
        self,
    ) -> google.protobuf.internal.containers.MessageMap[
        builtins.int, global___TaskQueueTypeInfo
    ]:
        """Task Queue info per Task Type. Key is the numerical value of the temporalio.api.enums.v1.TaskQueueType enum."""
    task_reachability: (
        temporalio.api.enums.v1.task_queue_pb2.BuildIdTaskReachability.ValueType
    )
    """Task Reachability is eventually consistent; there may be a delay until it converges to the most
    accurate value but it is designed in a way to take the more conservative side until it converges.
    For example REACHABLE is more conservative than CLOSED_WORKFLOWS_ONLY.

    Note: future activities who inherit their workflow's Build ID but not its Task Queue will not be
    accounted for reachability as server cannot know if they'll happen as they do not use
    assignment rules of their Task Queue. Same goes for Child Workflows or Continue-As-New Workflows
    who inherit the parent/previous workflow's Build ID but not its Task Queue. In those cases, make
    sure to query reachability for the parent/previous workflow's Task Queue as well.
    """
    def __init__(
        self,
        *,
        types_info: collections.abc.Mapping[builtins.int, global___TaskQueueTypeInfo]
        | None = ...,
        task_reachability: temporalio.api.enums.v1.task_queue_pb2.BuildIdTaskReachability.ValueType = ...,
    ) -> None: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "task_reachability", b"task_reachability", "types_info", b"types_info"
        ],
    ) -> None: ...

global___TaskQueueVersionInfo = TaskQueueVersionInfo

class TaskQueueTypeInfo(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    POLLERS_FIELD_NUMBER: builtins.int
    STATS_FIELD_NUMBER: builtins.int
    @property
    def pollers(
        self,
    ) -> google.protobuf.internal.containers.RepeatedCompositeFieldContainer[
        global___PollerInfo
    ]:
        """Unversioned workers (with `useVersioning=false`) are reported in unversioned result even if they set a Build ID."""
    @property
    def stats(self) -> global___TaskQueueStats: ...
    def __init__(
        self,
        *,
        pollers: collections.abc.Iterable[global___PollerInfo] | None = ...,
        stats: global___TaskQueueStats | None = ...,
    ) -> None: ...
    def HasField(
        self, field_name: typing_extensions.Literal["stats", b"stats"]
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal["pollers", b"pollers", "stats", b"stats"],
    ) -> None: ...

global___TaskQueueTypeInfo = TaskQueueTypeInfo

class TaskQueueStats(google.protobuf.message.Message):
    """For workflow task queues, we only report the normal queue stats, not sticky queues. This means the stats
    reported here do not count all workflow tasks. However, because the tasks queued in sticky queues only remain
    valid for a few seconds, the inaccuracy becomes less significant as the backlog age grows.
    """

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    APPROXIMATE_BACKLOG_COUNT_FIELD_NUMBER: builtins.int
    APPROXIMATE_BACKLOG_AGE_FIELD_NUMBER: builtins.int
    TASKS_ADD_RATE_FIELD_NUMBER: builtins.int
    TASKS_DISPATCH_RATE_FIELD_NUMBER: builtins.int
    approximate_backlog_count: builtins.int
    """The approximate number of tasks backlogged in this task queue. May count expired tasks but eventually converges
    to the right value.
    """
    @property
    def approximate_backlog_age(self) -> google.protobuf.duration_pb2.Duration:
        """Approximate age of the oldest task in the backlog based on the create timestamp of the task at the head of the queue."""
    tasks_add_rate: builtins.float
    """Approximate tasks per second added to the task queue based on activity within a fixed window. This includes both backlogged and
    sync-matched tasks.
    """
    tasks_dispatch_rate: builtins.float
    """Approximate tasks per second dispatched to workers based on activity within a fixed window. This includes both backlogged and
    sync-matched tasks.
    """
    def __init__(
        self,
        *,
        approximate_backlog_count: builtins.int = ...,
        approximate_backlog_age: google.protobuf.duration_pb2.Duration | None = ...,
        tasks_add_rate: builtins.float = ...,
        tasks_dispatch_rate: builtins.float = ...,
    ) -> None: ...
    def HasField(
        self,
        field_name: typing_extensions.Literal[
            "approximate_backlog_age", b"approximate_backlog_age"
        ],
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "approximate_backlog_age",
            b"approximate_backlog_age",
            "approximate_backlog_count",
            b"approximate_backlog_count",
            "tasks_add_rate",
            b"tasks_add_rate",
            "tasks_dispatch_rate",
            b"tasks_dispatch_rate",
        ],
    ) -> None: ...

global___TaskQueueStats = TaskQueueStats

class TaskQueueStatus(google.protobuf.message.Message):
    """Deprecated. Use `InternalTaskQueueStatus`. This is kept until `DescribeTaskQueue` supports legacy behavior."""

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    BACKLOG_COUNT_HINT_FIELD_NUMBER: builtins.int
    READ_LEVEL_FIELD_NUMBER: builtins.int
    ACK_LEVEL_FIELD_NUMBER: builtins.int
    RATE_PER_SECOND_FIELD_NUMBER: builtins.int
    TASK_ID_BLOCK_FIELD_NUMBER: builtins.int
    backlog_count_hint: builtins.int
    read_level: builtins.int
    ack_level: builtins.int
    rate_per_second: builtins.float
    @property
    def task_id_block(self) -> global___TaskIdBlock: ...
    def __init__(
        self,
        *,
        backlog_count_hint: builtins.int = ...,
        read_level: builtins.int = ...,
        ack_level: builtins.int = ...,
        rate_per_second: builtins.float = ...,
        task_id_block: global___TaskIdBlock | None = ...,
    ) -> None: ...
    def HasField(
        self, field_name: typing_extensions.Literal["task_id_block", b"task_id_block"]
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "ack_level",
            b"ack_level",
            "backlog_count_hint",
            b"backlog_count_hint",
            "rate_per_second",
            b"rate_per_second",
            "read_level",
            b"read_level",
            "task_id_block",
            b"task_id_block",
        ],
    ) -> None: ...

global___TaskQueueStatus = TaskQueueStatus

class TaskIdBlock(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    START_ID_FIELD_NUMBER: builtins.int
    END_ID_FIELD_NUMBER: builtins.int
    start_id: builtins.int
    end_id: builtins.int
    def __init__(
        self,
        *,
        start_id: builtins.int = ...,
        end_id: builtins.int = ...,
    ) -> None: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "end_id", b"end_id", "start_id", b"start_id"
        ],
    ) -> None: ...

global___TaskIdBlock = TaskIdBlock

class TaskQueuePartitionMetadata(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    KEY_FIELD_NUMBER: builtins.int
    OWNER_HOST_NAME_FIELD_NUMBER: builtins.int
    key: builtins.str
    owner_host_name: builtins.str
    def __init__(
        self,
        *,
        key: builtins.str = ...,
        owner_host_name: builtins.str = ...,
    ) -> None: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "key", b"key", "owner_host_name", b"owner_host_name"
        ],
    ) -> None: ...

global___TaskQueuePartitionMetadata = TaskQueuePartitionMetadata

class PollerInfo(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    LAST_ACCESS_TIME_FIELD_NUMBER: builtins.int
    IDENTITY_FIELD_NUMBER: builtins.int
    RATE_PER_SECOND_FIELD_NUMBER: builtins.int
    WORKER_VERSION_CAPABILITIES_FIELD_NUMBER: builtins.int
    @property
    def last_access_time(self) -> google.protobuf.timestamp_pb2.Timestamp: ...
    identity: builtins.str
    rate_per_second: builtins.float
    @property
    def worker_version_capabilities(
        self,
    ) -> temporalio.api.common.v1.message_pb2.WorkerVersionCapabilities:
        """If a worker has opted into the worker versioning feature while polling, its capabilities will
        appear here.
        """
    def __init__(
        self,
        *,
        last_access_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
        identity: builtins.str = ...,
        rate_per_second: builtins.float = ...,
        worker_version_capabilities: temporalio.api.common.v1.message_pb2.WorkerVersionCapabilities
        | None = ...,
    ) -> None: ...
    def HasField(
        self,
        field_name: typing_extensions.Literal[
            "last_access_time",
            b"last_access_time",
            "worker_version_capabilities",
            b"worker_version_capabilities",
        ],
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "identity",
            b"identity",
            "last_access_time",
            b"last_access_time",
            "rate_per_second",
            b"rate_per_second",
            "worker_version_capabilities",
            b"worker_version_capabilities",
        ],
    ) -> None: ...

global___PollerInfo = PollerInfo

class StickyExecutionAttributes(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    WORKER_TASK_QUEUE_FIELD_NUMBER: builtins.int
    SCHEDULE_TO_START_TIMEOUT_FIELD_NUMBER: builtins.int
    @property
    def worker_task_queue(self) -> global___TaskQueue: ...
    @property
    def schedule_to_start_timeout(self) -> google.protobuf.duration_pb2.Duration:
        """(-- api-linter: core::0140::prepositions=disabled
        aip.dev/not-precedent: "to" is used to indicate interval. --)
        """
    def __init__(
        self,
        *,
        worker_task_queue: global___TaskQueue | None = ...,
        schedule_to_start_timeout: google.protobuf.duration_pb2.Duration | None = ...,
    ) -> None: ...
    def HasField(
        self,
        field_name: typing_extensions.Literal[
            "schedule_to_start_timeout",
            b"schedule_to_start_timeout",
            "worker_task_queue",
            b"worker_task_queue",
        ],
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "schedule_to_start_timeout",
            b"schedule_to_start_timeout",
            "worker_task_queue",
            b"worker_task_queue",
        ],
    ) -> None: ...

global___StickyExecutionAttributes = StickyExecutionAttributes

class CompatibleVersionSet(google.protobuf.message.Message):
    """Used by the worker versioning APIs, represents an unordered set of one or more versions which are
    considered to be compatible with each other. Currently the versions are always worker build IDs.
    """

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    BUILD_IDS_FIELD_NUMBER: builtins.int
    @property
    def build_ids(
        self,
    ) -> google.protobuf.internal.containers.RepeatedScalarFieldContainer[builtins.str]:
        """All the compatible versions, unordered, except for the last element, which is considered the set "default"."""
    def __init__(
        self,
        *,
        build_ids: collections.abc.Iterable[builtins.str] | None = ...,
    ) -> None: ...
    def ClearField(
        self, field_name: typing_extensions.Literal["build_ids", b"build_ids"]
    ) -> None: ...

global___CompatibleVersionSet = CompatibleVersionSet

class TaskQueueReachability(google.protobuf.message.Message):
    """Reachability of tasks for a worker on a single task queue."""

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    TASK_QUEUE_FIELD_NUMBER: builtins.int
    REACHABILITY_FIELD_NUMBER: builtins.int
    task_queue: builtins.str
    @property
    def reachability(
        self,
    ) -> google.protobuf.internal.containers.RepeatedScalarFieldContainer[
        temporalio.api.enums.v1.task_queue_pb2.TaskReachability.ValueType
    ]:
        """Task reachability for a worker in a single task queue.
        See the TaskReachability docstring for information about each enum variant.
        If reachability is empty, this worker is considered unreachable in this task queue.
        """
    def __init__(
        self,
        *,
        task_queue: builtins.str = ...,
        reachability: collections.abc.Iterable[
            temporalio.api.enums.v1.task_queue_pb2.TaskReachability.ValueType
        ]
        | None = ...,
    ) -> None: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "reachability", b"reachability", "task_queue", b"task_queue"
        ],
    ) -> None: ...

global___TaskQueueReachability = TaskQueueReachability

class BuildIdReachability(google.protobuf.message.Message):
    """Reachability of tasks for a worker by build id, in one or more task queues."""

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    BUILD_ID_FIELD_NUMBER: builtins.int
    TASK_QUEUE_REACHABILITY_FIELD_NUMBER: builtins.int
    build_id: builtins.str
    """A build id or empty if unversioned."""
    @property
    def task_queue_reachability(
        self,
    ) -> google.protobuf.internal.containers.RepeatedCompositeFieldContainer[
        global___TaskQueueReachability
    ]:
        """Reachability per task queue."""
    def __init__(
        self,
        *,
        build_id: builtins.str = ...,
        task_queue_reachability: collections.abc.Iterable[
            global___TaskQueueReachability
        ]
        | None = ...,
    ) -> None: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "build_id",
            b"build_id",
            "task_queue_reachability",
            b"task_queue_reachability",
        ],
    ) -> None: ...

global___BuildIdReachability = BuildIdReachability

class RampByPercentage(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    RAMP_PERCENTAGE_FIELD_NUMBER: builtins.int
    ramp_percentage: builtins.float
    """Acceptable range is [0,100)."""
    def __init__(
        self,
        *,
        ramp_percentage: builtins.float = ...,
    ) -> None: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal["ramp_percentage", b"ramp_percentage"],
    ) -> None: ...

global___RampByPercentage = RampByPercentage

class BuildIdAssignmentRule(google.protobuf.message.Message):
    """Assignment rules are applied to *new* Workflow and Activity executions at
    schedule time to assign them to a Build ID.

    Assignment rules will not be used in the following cases:
       - Child Workflows or Continue-As-New Executions who inherit their
         parent/previous Workflow's assigned Build ID (by setting the
         `inherit_build_id` flag - default behavior in SDKs when the same Task Queue
         is used.)
       - An Activity that inherits the assigned Build ID of its Workflow (by
         setting the `use_workflow_build_id` flag - default behavior in SDKs
         when the same Task Queue is used.)

    In absence of (applicable) redirect rules (`CompatibleBuildIdRedirectRule`s)
    the task will be dispatched to Workers of the Build ID determined by the
    assignment rules (or inherited). Otherwise, the final Build ID will be
    determined by the redirect rules.

    Once a Workflow completes its first Workflow Task in a particular Build ID it
    stays in that Build ID regardless of changes to assignment rules. Redirect
    rules can be used to move the workflow to another compatible Build ID.

    When using Worker Versioning on a Task Queue, in the steady state,
    there should typically be a single assignment rule to send all new executions
    to the latest Build ID. Existence of at least one such "unconditional"
    rule at all times is enforces by the system, unless the `force` flag is used
    by the user when replacing/deleting these rules (for exceptional cases).

    During a deployment, one or more additional rules can be added to assign a
    subset of the tasks to a new Build ID based on a "ramp percentage".

    When there are multiple assignment rules for a Task Queue, the rules are
    evaluated in order, starting from index 0. The first applicable rule will be
    applied and the rest will be ignored.

    In the event that no assignment rule is applicable on a task (or the Task
    Queue is simply not versioned), the tasks will be dispatched to an
    unversioned Worker.
    """

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    TARGET_BUILD_ID_FIELD_NUMBER: builtins.int
    PERCENTAGE_RAMP_FIELD_NUMBER: builtins.int
    target_build_id: builtins.str
    @property
    def percentage_ramp(self) -> global___RampByPercentage:
        """This ramp is useful for gradual Blue/Green deployments (and similar)
        where you want to send a certain portion of the traffic to the target
        Build ID.
        """
    def __init__(
        self,
        *,
        target_build_id: builtins.str = ...,
        percentage_ramp: global___RampByPercentage | None = ...,
    ) -> None: ...
    def HasField(
        self,
        field_name: typing_extensions.Literal[
            "percentage_ramp", b"percentage_ramp", "ramp", b"ramp"
        ],
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "percentage_ramp",
            b"percentage_ramp",
            "ramp",
            b"ramp",
            "target_build_id",
            b"target_build_id",
        ],
    ) -> None: ...
    def WhichOneof(
        self, oneof_group: typing_extensions.Literal["ramp", b"ramp"]
    ) -> typing_extensions.Literal["percentage_ramp"] | None: ...

global___BuildIdAssignmentRule = BuildIdAssignmentRule

class CompatibleBuildIdRedirectRule(google.protobuf.message.Message):
    """These rules apply to tasks assigned to a particular Build ID
    (`source_build_id`) to redirect them to another *compatible* Build ID
    (`target_build_id`).

    It is user's responsibility to ensure that the target Build ID is compatible
    with the source Build ID (e.g. by using the Patching API).

    Most deployments are not expected to need these rules, however following
    situations can greatly benefit from redirects:
     - Need to move long-running Workflow Executions from an old Build ID to a
       newer one.
     - Need to hotfix some broken or stuck Workflow Executions.

    In steady state, redirect rules are beneficial when dealing with old
    Executions ran on now-decommissioned Build IDs:
     - To redirecting the Workflow Queries to the current (compatible) Build ID.
     - To be able to Reset an old Execution so it can run on the current
       (compatible) Build ID.

    Redirect rules can be chained.
    """

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    SOURCE_BUILD_ID_FIELD_NUMBER: builtins.int
    TARGET_BUILD_ID_FIELD_NUMBER: builtins.int
    source_build_id: builtins.str
    target_build_id: builtins.str
    """Target Build ID must be compatible with the Source Build ID; that is it
    must be able to process event histories made by the Source Build ID by
    using [Patching](https://docs.temporal.io/workflows#patching) or other
    means.
    """
    def __init__(
        self,
        *,
        source_build_id: builtins.str = ...,
        target_build_id: builtins.str = ...,
    ) -> None: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "source_build_id", b"source_build_id", "target_build_id", b"target_build_id"
        ],
    ) -> None: ...

global___CompatibleBuildIdRedirectRule = CompatibleBuildIdRedirectRule

class TimestampedBuildIdAssignmentRule(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    RULE_FIELD_NUMBER: builtins.int
    CREATE_TIME_FIELD_NUMBER: builtins.int
    @property
    def rule(self) -> global___BuildIdAssignmentRule: ...
    @property
    def create_time(self) -> google.protobuf.timestamp_pb2.Timestamp: ...
    def __init__(
        self,
        *,
        rule: global___BuildIdAssignmentRule | None = ...,
        create_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
    ) -> None: ...
    def HasField(
        self,
        field_name: typing_extensions.Literal[
            "create_time", b"create_time", "rule", b"rule"
        ],
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "create_time", b"create_time", "rule", b"rule"
        ],
    ) -> None: ...

global___TimestampedBuildIdAssignmentRule = TimestampedBuildIdAssignmentRule

class TimestampedCompatibleBuildIdRedirectRule(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    RULE_FIELD_NUMBER: builtins.int
    CREATE_TIME_FIELD_NUMBER: builtins.int
    @property
    def rule(self) -> global___CompatibleBuildIdRedirectRule: ...
    @property
    def create_time(self) -> google.protobuf.timestamp_pb2.Timestamp: ...
    def __init__(
        self,
        *,
        rule: global___CompatibleBuildIdRedirectRule | None = ...,
        create_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
    ) -> None: ...
    def HasField(
        self,
        field_name: typing_extensions.Literal[
            "create_time", b"create_time", "rule", b"rule"
        ],
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "create_time", b"create_time", "rule", b"rule"
        ],
    ) -> None: ...

global___TimestampedCompatibleBuildIdRedirectRule = (
    TimestampedCompatibleBuildIdRedirectRule
)
