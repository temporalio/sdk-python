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
import google.protobuf.internal.containers
import google.protobuf.message
import google.protobuf.timestamp_pb2

import temporalio.api.common.v1.message_pb2
import temporalio.api.enums.v1.deployment_pb2
import temporalio.api.enums.v1.task_queue_pb2

if sys.version_info >= (3, 8):
    import typing as typing_extensions
else:
    import typing_extensions

DESCRIPTOR: google.protobuf.descriptor.FileDescriptor

class WorkerDeploymentOptions(google.protobuf.message.Message):
    """Worker Deployment options set in SDK that need to be sent to server in every poll.
    Experimental. Worker Deployments are experimental and might significantly change in the future.
    """

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    DEPLOYMENT_NAME_FIELD_NUMBER: builtins.int
    BUILD_ID_FIELD_NUMBER: builtins.int
    WORKER_VERSIONING_MODE_FIELD_NUMBER: builtins.int
    deployment_name: builtins.str
    """Required. Worker Deployment name."""
    build_id: builtins.str
    """The Build ID of the worker. Required when `worker_versioning_mode==VERSIONED`, in which case,
    the worker will be part of a Deployment Version identified by "<deployment_name>.<build_id>".
    """
    worker_versioning_mode: (
        temporalio.api.enums.v1.deployment_pb2.WorkerVersioningMode.ValueType
    )
    """Required. Versioning Mode for this worker. Must be the same for all workers with the
    same `deployment_name` and `build_id` combination, across all Task Queues.
    When `worker_versioning_mode==VERSIONED`, the worker will be part of a Deployment Version
    identified by "<deployment_name>.<build_id>".
    """
    def __init__(
        self,
        *,
        deployment_name: builtins.str = ...,
        build_id: builtins.str = ...,
        worker_versioning_mode: temporalio.api.enums.v1.deployment_pb2.WorkerVersioningMode.ValueType = ...,
    ) -> None: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "build_id",
            b"build_id",
            "deployment_name",
            b"deployment_name",
            "worker_versioning_mode",
            b"worker_versioning_mode",
        ],
    ) -> None: ...

global___WorkerDeploymentOptions = WorkerDeploymentOptions

class Deployment(google.protobuf.message.Message):
    """`Deployment` identifies a deployment of Temporal workers. The combination of deployment series
    name + build ID serves as the identifier. User can use `WorkerDeploymentOptions` in their worker
    programs to specify these values.
    Deprecated.
    """

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    SERIES_NAME_FIELD_NUMBER: builtins.int
    BUILD_ID_FIELD_NUMBER: builtins.int
    series_name: builtins.str
    """Different versions of the same worker service/application are related together by having a
    shared series name.
    Out of all deployments of a series, one can be designated as the current deployment, which
    receives new workflow executions and new tasks of workflows with
    `VERSIONING_BEHAVIOR_AUTO_UPGRADE` versioning behavior.
    """
    build_id: builtins.str
    """Build ID changes with each version of the worker when the worker program code and/or config
    changes.
    """
    def __init__(
        self,
        *,
        series_name: builtins.str = ...,
        build_id: builtins.str = ...,
    ) -> None: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "build_id", b"build_id", "series_name", b"series_name"
        ],
    ) -> None: ...

global___Deployment = Deployment

class DeploymentInfo(google.protobuf.message.Message):
    """`DeploymentInfo` holds information about a deployment. Deployment information is tracked
    automatically by server as soon as the first poll from that deployment reaches the server. There
    can be multiple task queue workers in a single deployment which are listed in this message.
    Deprecated.
    """

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    class MetadataEntry(google.protobuf.message.Message):
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

    class TaskQueueInfo(google.protobuf.message.Message):
        DESCRIPTOR: google.protobuf.descriptor.Descriptor

        NAME_FIELD_NUMBER: builtins.int
        TYPE_FIELD_NUMBER: builtins.int
        FIRST_POLLER_TIME_FIELD_NUMBER: builtins.int
        name: builtins.str
        type: temporalio.api.enums.v1.task_queue_pb2.TaskQueueType.ValueType
        @property
        def first_poller_time(self) -> google.protobuf.timestamp_pb2.Timestamp:
            """When server saw the first poller for this task queue in this deployment."""
        def __init__(
            self,
            *,
            name: builtins.str = ...,
            type: temporalio.api.enums.v1.task_queue_pb2.TaskQueueType.ValueType = ...,
            first_poller_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
        ) -> None: ...
        def HasField(
            self,
            field_name: typing_extensions.Literal[
                "first_poller_time", b"first_poller_time"
            ],
        ) -> builtins.bool: ...
        def ClearField(
            self,
            field_name: typing_extensions.Literal[
                "first_poller_time",
                b"first_poller_time",
                "name",
                b"name",
                "type",
                b"type",
            ],
        ) -> None: ...

    DEPLOYMENT_FIELD_NUMBER: builtins.int
    CREATE_TIME_FIELD_NUMBER: builtins.int
    TASK_QUEUE_INFOS_FIELD_NUMBER: builtins.int
    METADATA_FIELD_NUMBER: builtins.int
    IS_CURRENT_FIELD_NUMBER: builtins.int
    @property
    def deployment(self) -> global___Deployment: ...
    @property
    def create_time(self) -> google.protobuf.timestamp_pb2.Timestamp: ...
    @property
    def task_queue_infos(
        self,
    ) -> google.protobuf.internal.containers.RepeatedCompositeFieldContainer[
        global___DeploymentInfo.TaskQueueInfo
    ]: ...
    @property
    def metadata(
        self,
    ) -> google.protobuf.internal.containers.MessageMap[
        builtins.str, temporalio.api.common.v1.message_pb2.Payload
    ]:
        """A user-defined set of key-values. Can be updated as part of write operations to the
        deployment, such as `SetCurrentDeployment`.
        """
    is_current: builtins.bool
    """If this deployment is the current deployment of its deployment series."""
    def __init__(
        self,
        *,
        deployment: global___Deployment | None = ...,
        create_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
        task_queue_infos: collections.abc.Iterable[
            global___DeploymentInfo.TaskQueueInfo
        ]
        | None = ...,
        metadata: collections.abc.Mapping[
            builtins.str, temporalio.api.common.v1.message_pb2.Payload
        ]
        | None = ...,
        is_current: builtins.bool = ...,
    ) -> None: ...
    def HasField(
        self,
        field_name: typing_extensions.Literal[
            "create_time", b"create_time", "deployment", b"deployment"
        ],
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "create_time",
            b"create_time",
            "deployment",
            b"deployment",
            "is_current",
            b"is_current",
            "metadata",
            b"metadata",
            "task_queue_infos",
            b"task_queue_infos",
        ],
    ) -> None: ...

global___DeploymentInfo = DeploymentInfo

class UpdateDeploymentMetadata(google.protobuf.message.Message):
    """Used as part of Deployment write APIs to update metadata attached to a deployment.
    Deprecated.
    """

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    class UpsertEntriesEntry(google.protobuf.message.Message):
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

    UPSERT_ENTRIES_FIELD_NUMBER: builtins.int
    REMOVE_ENTRIES_FIELD_NUMBER: builtins.int
    @property
    def upsert_entries(
        self,
    ) -> google.protobuf.internal.containers.MessageMap[
        builtins.str, temporalio.api.common.v1.message_pb2.Payload
    ]: ...
    @property
    def remove_entries(
        self,
    ) -> google.protobuf.internal.containers.RepeatedScalarFieldContainer[builtins.str]:
        """List of keys to remove from the metadata."""
    def __init__(
        self,
        *,
        upsert_entries: collections.abc.Mapping[
            builtins.str, temporalio.api.common.v1.message_pb2.Payload
        ]
        | None = ...,
        remove_entries: collections.abc.Iterable[builtins.str] | None = ...,
    ) -> None: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "remove_entries", b"remove_entries", "upsert_entries", b"upsert_entries"
        ],
    ) -> None: ...

global___UpdateDeploymentMetadata = UpdateDeploymentMetadata

class DeploymentListInfo(google.protobuf.message.Message):
    """DeploymentListInfo is an abbreviated set of fields from DeploymentInfo that's returned in
    ListDeployments.
    Deprecated.
    """

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    DEPLOYMENT_FIELD_NUMBER: builtins.int
    CREATE_TIME_FIELD_NUMBER: builtins.int
    IS_CURRENT_FIELD_NUMBER: builtins.int
    @property
    def deployment(self) -> global___Deployment: ...
    @property
    def create_time(self) -> google.protobuf.timestamp_pb2.Timestamp: ...
    is_current: builtins.bool
    """If this deployment is the current deployment of its deployment series."""
    def __init__(
        self,
        *,
        deployment: global___Deployment | None = ...,
        create_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
        is_current: builtins.bool = ...,
    ) -> None: ...
    def HasField(
        self,
        field_name: typing_extensions.Literal[
            "create_time", b"create_time", "deployment", b"deployment"
        ],
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "create_time",
            b"create_time",
            "deployment",
            b"deployment",
            "is_current",
            b"is_current",
        ],
    ) -> None: ...

global___DeploymentListInfo = DeploymentListInfo

class WorkerDeploymentVersionInfo(google.protobuf.message.Message):
    """A Worker Deployment Version (Version, for short) represents all workers of the same
    code and config within a Deployment. Workers of the same Version are expected to
    behave exactly the same so when executions move between them there are no
    non-determinism issues.
    Worker Deployment Versions are created in Temporal server automatically when
    their first poller arrives to the server.
    Experimental. Worker Deployments are experimental and might significantly change in the future.
    """

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    class VersionTaskQueueInfo(google.protobuf.message.Message):
        DESCRIPTOR: google.protobuf.descriptor.Descriptor

        NAME_FIELD_NUMBER: builtins.int
        TYPE_FIELD_NUMBER: builtins.int
        name: builtins.str
        type: temporalio.api.enums.v1.task_queue_pb2.TaskQueueType.ValueType
        def __init__(
            self,
            *,
            name: builtins.str = ...,
            type: temporalio.api.enums.v1.task_queue_pb2.TaskQueueType.ValueType = ...,
        ) -> None: ...
        def ClearField(
            self,
            field_name: typing_extensions.Literal["name", b"name", "type", b"type"],
        ) -> None: ...

    VERSION_FIELD_NUMBER: builtins.int
    DEPLOYMENT_NAME_FIELD_NUMBER: builtins.int
    CREATE_TIME_FIELD_NUMBER: builtins.int
    ROUTING_CHANGED_TIME_FIELD_NUMBER: builtins.int
    CURRENT_SINCE_TIME_FIELD_NUMBER: builtins.int
    RAMPING_SINCE_TIME_FIELD_NUMBER: builtins.int
    RAMP_PERCENTAGE_FIELD_NUMBER: builtins.int
    TASK_QUEUE_INFOS_FIELD_NUMBER: builtins.int
    DRAINAGE_INFO_FIELD_NUMBER: builtins.int
    METADATA_FIELD_NUMBER: builtins.int
    version: builtins.str
    """The fully-qualified string representation of the version, in the form "<deployment_name>.<build_id>"."""
    deployment_name: builtins.str
    @property
    def create_time(self) -> google.protobuf.timestamp_pb2.Timestamp: ...
    @property
    def routing_changed_time(self) -> google.protobuf.timestamp_pb2.Timestamp:
        """Last time `current_since_time`, `ramping_since_time, or `ramp_percentage` of this version changed."""
    @property
    def current_since_time(self) -> google.protobuf.timestamp_pb2.Timestamp:
        """(-- api-linter: core::0140::prepositions=disabled
            aip.dev/not-precedent: 'Since' captures the field semantics despite being a preposition. --)
        Nil if not current.
        """
    @property
    def ramping_since_time(self) -> google.protobuf.timestamp_pb2.Timestamp:
        """(-- api-linter: core::0140::prepositions=disabled
            aip.dev/not-precedent: 'Since' captures the field semantics despite being a preposition. --)
        Nil if not ramping. Updated when the version first starts ramping, not on each ramp change.
        """
    ramp_percentage: builtins.float
    """Range: [0, 100]. Must be zero if the version is not ramping (i.e. `ramping_since_time` is nil).
    Can be in the range [0, 100] if the version is ramping.
    """
    @property
    def task_queue_infos(
        self,
    ) -> google.protobuf.internal.containers.RepeatedCompositeFieldContainer[
        global___WorkerDeploymentVersionInfo.VersionTaskQueueInfo
    ]:
        """All the Task Queues that have ever polled from this Deployment version."""
    @property
    def drainage_info(self) -> global___VersionDrainageInfo:
        """Helps user determine when it is safe to decommission the workers of this
        Version. Not present when version is current or ramping.
        Current limitations:
        - Not supported for Unversioned mode.
        - Periodically refreshed, may have delays up to few minutes (consult the
          last_checked_time value).
        - Refreshed only when version is not current or ramping AND the status is not
          "drained" yet.
        - Once the status is changed to "drained", it is not changed until the Version
          becomes Current or Ramping again, at which time the drainage info is cleared.
          This means if the Version is "drained" but new workflows are sent to it via
          Pinned Versioning Override, the status does not account for those Pinned-override
          executions and remains "drained".
        """
    @property
    def metadata(self) -> global___VersionMetadata:
        """Arbitrary user-provided metadata attached to this version."""
    def __init__(
        self,
        *,
        version: builtins.str = ...,
        deployment_name: builtins.str = ...,
        create_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
        routing_changed_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
        current_since_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
        ramping_since_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
        ramp_percentage: builtins.float = ...,
        task_queue_infos: collections.abc.Iterable[
            global___WorkerDeploymentVersionInfo.VersionTaskQueueInfo
        ]
        | None = ...,
        drainage_info: global___VersionDrainageInfo | None = ...,
        metadata: global___VersionMetadata | None = ...,
    ) -> None: ...
    def HasField(
        self,
        field_name: typing_extensions.Literal[
            "create_time",
            b"create_time",
            "current_since_time",
            b"current_since_time",
            "drainage_info",
            b"drainage_info",
            "metadata",
            b"metadata",
            "ramping_since_time",
            b"ramping_since_time",
            "routing_changed_time",
            b"routing_changed_time",
        ],
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "create_time",
            b"create_time",
            "current_since_time",
            b"current_since_time",
            "deployment_name",
            b"deployment_name",
            "drainage_info",
            b"drainage_info",
            "metadata",
            b"metadata",
            "ramp_percentage",
            b"ramp_percentage",
            "ramping_since_time",
            b"ramping_since_time",
            "routing_changed_time",
            b"routing_changed_time",
            "task_queue_infos",
            b"task_queue_infos",
            "version",
            b"version",
        ],
    ) -> None: ...

global___WorkerDeploymentVersionInfo = WorkerDeploymentVersionInfo

class VersionDrainageInfo(google.protobuf.message.Message):
    """Information about workflow drainage to help the user determine when it is safe
    to decommission a Version. Not present while version is current or ramping.
    Experimental. Worker Deployments are experimental and might significantly change in the future.
    """

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    STATUS_FIELD_NUMBER: builtins.int
    LAST_CHANGED_TIME_FIELD_NUMBER: builtins.int
    LAST_CHECKED_TIME_FIELD_NUMBER: builtins.int
    status: temporalio.api.enums.v1.deployment_pb2.VersionDrainageStatus.ValueType
    """Set to DRAINING when the version first stops accepting new executions (is no longer current or ramping).
    Set to DRAINED when no more open pinned workflows exist on this version.
    """
    @property
    def last_changed_time(self) -> google.protobuf.timestamp_pb2.Timestamp:
        """Last time the drainage status changed."""
    @property
    def last_checked_time(self) -> google.protobuf.timestamp_pb2.Timestamp:
        """Last time the system checked for drainage of this version."""
    def __init__(
        self,
        *,
        status: temporalio.api.enums.v1.deployment_pb2.VersionDrainageStatus.ValueType = ...,
        last_changed_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
        last_checked_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
    ) -> None: ...
    def HasField(
        self,
        field_name: typing_extensions.Literal[
            "last_changed_time",
            b"last_changed_time",
            "last_checked_time",
            b"last_checked_time",
        ],
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "last_changed_time",
            b"last_changed_time",
            "last_checked_time",
            b"last_checked_time",
            "status",
            b"status",
        ],
    ) -> None: ...

global___VersionDrainageInfo = VersionDrainageInfo

class WorkerDeploymentInfo(google.protobuf.message.Message):
    """A Worker Deployment (Deployment, for short) represents all workers serving
    a shared set of Task Queues. Typically, a Deployment represents one service or
    application.
    A Deployment contains multiple Deployment Versions, each representing a different
    version of workers. (see documentation of WorkerDeploymentVersionInfo)
    Deployment records are created in Temporal server automatically when their
    first poller arrives to the server.
    Experimental. Worker Deployments are experimental and might significantly change in the future.
    """

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    class WorkerDeploymentVersionSummary(google.protobuf.message.Message):
        DESCRIPTOR: google.protobuf.descriptor.Descriptor

        VERSION_FIELD_NUMBER: builtins.int
        CREATE_TIME_FIELD_NUMBER: builtins.int
        DRAINAGE_STATUS_FIELD_NUMBER: builtins.int
        version: builtins.str
        """The fully-qualified string representation of the version, in the form "<deployment_name>.<build_id>"."""
        @property
        def create_time(self) -> google.protobuf.timestamp_pb2.Timestamp: ...
        drainage_status: (
            temporalio.api.enums.v1.deployment_pb2.VersionDrainageStatus.ValueType
        )
        def __init__(
            self,
            *,
            version: builtins.str = ...,
            create_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
            drainage_status: temporalio.api.enums.v1.deployment_pb2.VersionDrainageStatus.ValueType = ...,
        ) -> None: ...
        def HasField(
            self, field_name: typing_extensions.Literal["create_time", b"create_time"]
        ) -> builtins.bool: ...
        def ClearField(
            self,
            field_name: typing_extensions.Literal[
                "create_time",
                b"create_time",
                "drainage_status",
                b"drainage_status",
                "version",
                b"version",
            ],
        ) -> None: ...

    NAME_FIELD_NUMBER: builtins.int
    VERSION_SUMMARIES_FIELD_NUMBER: builtins.int
    CREATE_TIME_FIELD_NUMBER: builtins.int
    ROUTING_CONFIG_FIELD_NUMBER: builtins.int
    LAST_MODIFIER_IDENTITY_FIELD_NUMBER: builtins.int
    name: builtins.str
    """Identifies a Worker Deployment. Must be unique within the namespace."""
    @property
    def version_summaries(
        self,
    ) -> google.protobuf.internal.containers.RepeatedCompositeFieldContainer[
        global___WorkerDeploymentInfo.WorkerDeploymentVersionSummary
    ]:
        """Deployment Versions that are currently tracked in this Deployment. A DeploymentVersion will be
        cleaned up automatically if all the following conditions meet:
        - It does not receive new executions (is not current or ramping)
        - It has no active pollers (see WorkerDeploymentVersionInfo.pollers_status)
        - It is drained (see WorkerDeploymentVersionInfo.drainage_status)
        """
    @property
    def create_time(self) -> google.protobuf.timestamp_pb2.Timestamp: ...
    @property
    def routing_config(self) -> global___RoutingConfig: ...
    last_modifier_identity: builtins.str
    """Identity of the last client who modified the configuration of this Deployment. Set to the
    `identity` value sent by APIs such as `SetWorkerDeploymentCurrentVersion` and
    `SetWorkerDeploymentRampingVersion`.
    """
    def __init__(
        self,
        *,
        name: builtins.str = ...,
        version_summaries: collections.abc.Iterable[
            global___WorkerDeploymentInfo.WorkerDeploymentVersionSummary
        ]
        | None = ...,
        create_time: google.protobuf.timestamp_pb2.Timestamp | None = ...,
        routing_config: global___RoutingConfig | None = ...,
        last_modifier_identity: builtins.str = ...,
    ) -> None: ...
    def HasField(
        self,
        field_name: typing_extensions.Literal[
            "create_time", b"create_time", "routing_config", b"routing_config"
        ],
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "create_time",
            b"create_time",
            "last_modifier_identity",
            b"last_modifier_identity",
            "name",
            b"name",
            "routing_config",
            b"routing_config",
            "version_summaries",
            b"version_summaries",
        ],
    ) -> None: ...

global___WorkerDeploymentInfo = WorkerDeploymentInfo

class VersionMetadata(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    class EntriesEntry(google.protobuf.message.Message):
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

    ENTRIES_FIELD_NUMBER: builtins.int
    @property
    def entries(
        self,
    ) -> google.protobuf.internal.containers.MessageMap[
        builtins.str, temporalio.api.common.v1.message_pb2.Payload
    ]:
        """Arbitrary key-values."""
    def __init__(
        self,
        *,
        entries: collections.abc.Mapping[
            builtins.str, temporalio.api.common.v1.message_pb2.Payload
        ]
        | None = ...,
    ) -> None: ...
    def ClearField(
        self, field_name: typing_extensions.Literal["entries", b"entries"]
    ) -> None: ...

global___VersionMetadata = VersionMetadata

class RoutingConfig(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    CURRENT_VERSION_FIELD_NUMBER: builtins.int
    RAMPING_VERSION_FIELD_NUMBER: builtins.int
    RAMPING_VERSION_PERCENTAGE_FIELD_NUMBER: builtins.int
    CURRENT_VERSION_CHANGED_TIME_FIELD_NUMBER: builtins.int
    RAMPING_VERSION_CHANGED_TIME_FIELD_NUMBER: builtins.int
    RAMPING_VERSION_PERCENTAGE_CHANGED_TIME_FIELD_NUMBER: builtins.int
    current_version: builtins.str
    """Always present. Specifies which Deployment Version should should receive new workflow
    executions and tasks of existing unversioned or AutoUpgrade workflows.
    Can be one of the following:
    - A Deployment Version identifier in the form "<deployment_name>.<build_id>".
    - Or, the "__unversioned__" special value, to represent all the unversioned workers (those
      with `UNVERSIONED` (or unspecified) `WorkerVersioningMode`.)
    Note: Current Version is overridden by the Ramping Version for a portion of traffic when a ramp
    is set (see `ramping_version`.)
    """
    ramping_version: builtins.str
    """When present, it means the traffic is being shifted from the Current Version to the Ramping
    Version.
    Must always be different from Current Version. Can be one of the following:
    - A Deployment Version identifier in the form "<deployment_name>.<build_id>".
    - Or, the "__unversioned__" special value, to represent all the unversioned workers (those
      with `UNVERSIONED` (or unspecified) `WorkerVersioningMode`.)
    Note that it is possible to ramp from one Version to another Version, or from unversioned
    workers to a particular Version, or from a particular Version to unversioned workers.
    """
    ramping_version_percentage: builtins.float
    """Percentage of tasks that are routed to the Ramping Version instead of the Current Version.
    Valid range: [0, 100]. A 100% value means the Ramping Version is receiving full traffic but
    not yet "promoted" to be the Current Version, likely due to pending validations.
    """
    @property
    def current_version_changed_time(self) -> google.protobuf.timestamp_pb2.Timestamp:
        """Last time current version was changed."""
    @property
    def ramping_version_changed_time(self) -> google.protobuf.timestamp_pb2.Timestamp:
        """Last time ramping version was changed. Not updated if only the ramp percentage changes."""
    @property
    def ramping_version_percentage_changed_time(
        self,
    ) -> google.protobuf.timestamp_pb2.Timestamp:
        """Last time ramping version percentage was changed.
        If ramping version is changed, this is also updated, even if the percentage stays the same.
        """
    def __init__(
        self,
        *,
        current_version: builtins.str = ...,
        ramping_version: builtins.str = ...,
        ramping_version_percentage: builtins.float = ...,
        current_version_changed_time: google.protobuf.timestamp_pb2.Timestamp
        | None = ...,
        ramping_version_changed_time: google.protobuf.timestamp_pb2.Timestamp
        | None = ...,
        ramping_version_percentage_changed_time: google.protobuf.timestamp_pb2.Timestamp
        | None = ...,
    ) -> None: ...
    def HasField(
        self,
        field_name: typing_extensions.Literal[
            "current_version_changed_time",
            b"current_version_changed_time",
            "ramping_version_changed_time",
            b"ramping_version_changed_time",
            "ramping_version_percentage_changed_time",
            b"ramping_version_percentage_changed_time",
        ],
    ) -> builtins.bool: ...
    def ClearField(
        self,
        field_name: typing_extensions.Literal[
            "current_version",
            b"current_version",
            "current_version_changed_time",
            b"current_version_changed_time",
            "ramping_version",
            b"ramping_version",
            "ramping_version_changed_time",
            b"ramping_version_changed_time",
            "ramping_version_percentage",
            b"ramping_version_percentage",
            "ramping_version_percentage_changed_time",
            b"ramping_version_percentage_changed_time",
        ],
    ) -> None: ...

global___RoutingConfig = RoutingConfig
