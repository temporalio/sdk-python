# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: temporal/api/taskqueue/v1/message.proto
"""Generated protocol buffer code."""
from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection
from google.protobuf import symbol_database as _symbol_database

# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()


from google.protobuf import duration_pb2 as google_dot_protobuf_dot_duration__pb2
from google.protobuf import timestamp_pb2 as google_dot_protobuf_dot_timestamp__pb2
from google.protobuf import wrappers_pb2 as google_dot_protobuf_dot_wrappers__pb2

from temporalio.api.dependencies.gogoproto import (
    gogo_pb2 as dependencies_dot_gogoproto_dot_gogo__pb2,
)
from temporalio.api.enums.v1 import (
    task_queue_pb2 as temporal_dot_api_dot_enums_dot_v1_dot_task__queue__pb2,
)

DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(
    b'\n\'temporal/api/taskqueue/v1/message.proto\x12\x19temporal.api.taskqueue.v1\x1a\x1egoogle/protobuf/duration.proto\x1a\x1fgoogle/protobuf/timestamp.proto\x1a\x1egoogle/protobuf/wrappers.proto\x1a!dependencies/gogoproto/gogo.proto\x1a&temporal/api/enums/v1/task_queue.proto"M\n\tTaskQueue\x12\x0c\n\x04name\x18\x01 \x01(\t\x12\x32\n\x04kind\x18\x02 \x01(\x0e\x32$.temporal.api.enums.v1.TaskQueueKind"O\n\x11TaskQueueMetadata\x12:\n\x14max_tasks_per_second\x18\x01 \x01(\x0b\x32\x1c.google.protobuf.DoubleValue"\xac\x01\n\x0fTaskQueueStatus\x12\x1a\n\x12\x62\x61\x63klog_count_hint\x18\x01 \x01(\x03\x12\x12\n\nread_level\x18\x02 \x01(\x03\x12\x11\n\tack_level\x18\x03 \x01(\x03\x12\x17\n\x0frate_per_second\x18\x04 \x01(\x01\x12=\n\rtask_id_block\x18\x05 \x01(\x0b\x32&.temporal.api.taskqueue.v1.TaskIdBlock"/\n\x0bTaskIdBlock\x12\x10\n\x08start_id\x18\x01 \x01(\x03\x12\x0e\n\x06\x65nd_id\x18\x02 \x01(\x03"B\n\x1aTaskQueuePartitionMetadata\x12\x0b\n\x03key\x18\x01 \x01(\t\x12\x17\n\x0fowner_host_name\x18\x02 \x01(\t"\xb7\x01\n\nPollerInfo\x12:\n\x10last_access_time\x18\x01 \x01(\x0b\x32\x1a.google.protobuf.TimestampB\x04\x90\xdf\x1f\x01\x12\x10\n\x08identity\x18\x02 \x01(\t\x12\x17\n\x0frate_per_second\x18\x03 \x01(\x01\x12\x42\n\x14worker_versioning_id\x18\x04 \x01(\x0b\x32$.temporal.api.taskqueue.v1.VersionId"\xa0\x01\n\x19StickyExecutionAttributes\x12?\n\x11worker_task_queue\x18\x01 \x01(\x0b\x32$.temporal.api.taskqueue.v1.TaskQueue\x12\x42\n\x19schedule_to_start_timeout\x18\x02 \x01(\x0b\x32\x19.google.protobuf.DurationB\x04\x98\xdf\x1f\x01"\xd6\x01\n\rVersionIdNode\x12\x35\n\x07version\x18\x01 \x01(\x0b\x32$.temporal.api.taskqueue.v1.VersionId\x12\x45\n\x13previous_compatible\x18\x02 \x01(\x0b\x32(.temporal.api.taskqueue.v1.VersionIdNode\x12G\n\x15previous_incompatible\x18\x03 \x01(\x0b\x32(.temporal.api.taskqueue.v1.VersionIdNode"$\n\tVersionId\x12\x17\n\x0fworker_build_id\x18\x01 \x01(\tB\x94\x01\n\x1cio.temporal.api.taskqueue.v1B\x0cMessageProtoP\x01Z)go.temporal.io/api/taskqueue/v1;taskqueue\xaa\x02\x19Temporal.Api.TaskQueue.V1\xea\x02\x1cTemporal::Api::TaskQueue::V1b\x06proto3'
)


_TASKQUEUE = DESCRIPTOR.message_types_by_name["TaskQueue"]
_TASKQUEUEMETADATA = DESCRIPTOR.message_types_by_name["TaskQueueMetadata"]
_TASKQUEUESTATUS = DESCRIPTOR.message_types_by_name["TaskQueueStatus"]
_TASKIDBLOCK = DESCRIPTOR.message_types_by_name["TaskIdBlock"]
_TASKQUEUEPARTITIONMETADATA = DESCRIPTOR.message_types_by_name[
    "TaskQueuePartitionMetadata"
]
_POLLERINFO = DESCRIPTOR.message_types_by_name["PollerInfo"]
_STICKYEXECUTIONATTRIBUTES = DESCRIPTOR.message_types_by_name[
    "StickyExecutionAttributes"
]
_VERSIONIDNODE = DESCRIPTOR.message_types_by_name["VersionIdNode"]
_VERSIONID = DESCRIPTOR.message_types_by_name["VersionId"]
TaskQueue = _reflection.GeneratedProtocolMessageType(
    "TaskQueue",
    (_message.Message,),
    {
        "DESCRIPTOR": _TASKQUEUE,
        "__module__": "temporal.api.taskqueue.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.taskqueue.v1.TaskQueue)
    },
)
_sym_db.RegisterMessage(TaskQueue)

TaskQueueMetadata = _reflection.GeneratedProtocolMessageType(
    "TaskQueueMetadata",
    (_message.Message,),
    {
        "DESCRIPTOR": _TASKQUEUEMETADATA,
        "__module__": "temporal.api.taskqueue.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.taskqueue.v1.TaskQueueMetadata)
    },
)
_sym_db.RegisterMessage(TaskQueueMetadata)

TaskQueueStatus = _reflection.GeneratedProtocolMessageType(
    "TaskQueueStatus",
    (_message.Message,),
    {
        "DESCRIPTOR": _TASKQUEUESTATUS,
        "__module__": "temporal.api.taskqueue.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.taskqueue.v1.TaskQueueStatus)
    },
)
_sym_db.RegisterMessage(TaskQueueStatus)

TaskIdBlock = _reflection.GeneratedProtocolMessageType(
    "TaskIdBlock",
    (_message.Message,),
    {
        "DESCRIPTOR": _TASKIDBLOCK,
        "__module__": "temporal.api.taskqueue.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.taskqueue.v1.TaskIdBlock)
    },
)
_sym_db.RegisterMessage(TaskIdBlock)

TaskQueuePartitionMetadata = _reflection.GeneratedProtocolMessageType(
    "TaskQueuePartitionMetadata",
    (_message.Message,),
    {
        "DESCRIPTOR": _TASKQUEUEPARTITIONMETADATA,
        "__module__": "temporal.api.taskqueue.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.taskqueue.v1.TaskQueuePartitionMetadata)
    },
)
_sym_db.RegisterMessage(TaskQueuePartitionMetadata)

PollerInfo = _reflection.GeneratedProtocolMessageType(
    "PollerInfo",
    (_message.Message,),
    {
        "DESCRIPTOR": _POLLERINFO,
        "__module__": "temporal.api.taskqueue.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.taskqueue.v1.PollerInfo)
    },
)
_sym_db.RegisterMessage(PollerInfo)

StickyExecutionAttributes = _reflection.GeneratedProtocolMessageType(
    "StickyExecutionAttributes",
    (_message.Message,),
    {
        "DESCRIPTOR": _STICKYEXECUTIONATTRIBUTES,
        "__module__": "temporal.api.taskqueue.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.taskqueue.v1.StickyExecutionAttributes)
    },
)
_sym_db.RegisterMessage(StickyExecutionAttributes)

VersionIdNode = _reflection.GeneratedProtocolMessageType(
    "VersionIdNode",
    (_message.Message,),
    {
        "DESCRIPTOR": _VERSIONIDNODE,
        "__module__": "temporal.api.taskqueue.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.taskqueue.v1.VersionIdNode)
    },
)
_sym_db.RegisterMessage(VersionIdNode)

VersionId = _reflection.GeneratedProtocolMessageType(
    "VersionId",
    (_message.Message,),
    {
        "DESCRIPTOR": _VERSIONID,
        "__module__": "temporal.api.taskqueue.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.taskqueue.v1.VersionId)
    },
)
_sym_db.RegisterMessage(VersionId)

if _descriptor._USE_C_DESCRIPTORS == False:

    DESCRIPTOR._options = None
    DESCRIPTOR._serialized_options = b"\n\034io.temporal.api.taskqueue.v1B\014MessageProtoP\001Z)go.temporal.io/api/taskqueue/v1;taskqueue\252\002\031Temporal.Api.TaskQueue.V1\352\002\034Temporal::Api::TaskQueue::V1"
    _POLLERINFO.fields_by_name["last_access_time"]._options = None
    _POLLERINFO.fields_by_name[
        "last_access_time"
    ]._serialized_options = b"\220\337\037\001"
    _STICKYEXECUTIONATTRIBUTES.fields_by_name[
        "schedule_to_start_timeout"
    ]._options = None
    _STICKYEXECUTIONATTRIBUTES.fields_by_name[
        "schedule_to_start_timeout"
    ]._serialized_options = b"\230\337\037\001"
    _TASKQUEUE._serialized_start = 242
    _TASKQUEUE._serialized_end = 319
    _TASKQUEUEMETADATA._serialized_start = 321
    _TASKQUEUEMETADATA._serialized_end = 400
    _TASKQUEUESTATUS._serialized_start = 403
    _TASKQUEUESTATUS._serialized_end = 575
    _TASKIDBLOCK._serialized_start = 577
    _TASKIDBLOCK._serialized_end = 624
    _TASKQUEUEPARTITIONMETADATA._serialized_start = 626
    _TASKQUEUEPARTITIONMETADATA._serialized_end = 692
    _POLLERINFO._serialized_start = 695
    _POLLERINFO._serialized_end = 878
    _STICKYEXECUTIONATTRIBUTES._serialized_start = 881
    _STICKYEXECUTIONATTRIBUTES._serialized_end = 1041
    _VERSIONIDNODE._serialized_start = 1044
    _VERSIONIDNODE._serialized_end = 1258
    _VERSIONID._serialized_start = 1260
    _VERSIONID._serialized_end = 1296
# @@protoc_insertion_point(module_scope)
