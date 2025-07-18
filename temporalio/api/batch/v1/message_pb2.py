# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: temporal/api/batch/v1/message.proto
"""Generated protocol buffer code."""

from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection
from google.protobuf import symbol_database as _symbol_database

# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()


from google.protobuf import duration_pb2 as google_dot_protobuf_dot_duration__pb2
from google.protobuf import field_mask_pb2 as google_dot_protobuf_dot_field__mask__pb2
from google.protobuf import timestamp_pb2 as google_dot_protobuf_dot_timestamp__pb2

from temporalio.api.common.v1 import (
    message_pb2 as temporal_dot_api_dot_common_dot_v1_dot_message__pb2,
)
from temporalio.api.enums.v1 import (
    batch_operation_pb2 as temporal_dot_api_dot_enums_dot_v1_dot_batch__operation__pb2,
)
from temporalio.api.enums.v1 import (
    reset_pb2 as temporal_dot_api_dot_enums_dot_v1_dot_reset__pb2,
)
from temporalio.api.rules.v1 import (
    message_pb2 as temporal_dot_api_dot_rules_dot_v1_dot_message__pb2,
)
from temporalio.api.workflow.v1 import (
    message_pb2 as temporal_dot_api_dot_workflow_dot_v1_dot_message__pb2,
)

DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(
    b'\n#temporal/api/batch/v1/message.proto\x12\x15temporal.api.batch.v1\x1a\x1egoogle/protobuf/duration.proto\x1a google/protobuf/field_mask.proto\x1a\x1fgoogle/protobuf/timestamp.proto\x1a$temporal/api/common/v1/message.proto\x1a+temporal/api/enums/v1/batch_operation.proto\x1a!temporal/api/enums/v1/reset.proto\x1a#temporal/api/rules/v1/message.proto\x1a&temporal/api/workflow/v1/message.proto"\xbf\x01\n\x12\x42\x61tchOperationInfo\x12\x0e\n\x06job_id\x18\x01 \x01(\t\x12\x39\n\x05state\x18\x02 \x01(\x0e\x32*.temporal.api.enums.v1.BatchOperationState\x12.\n\nstart_time\x18\x03 \x01(\x0b\x32\x1a.google.protobuf.Timestamp\x12.\n\nclose_time\x18\x04 \x01(\x0b\x32\x1a.google.protobuf.Timestamp"`\n\x19\x42\x61tchOperationTermination\x12\x31\n\x07\x64\x65tails\x18\x01 \x01(\x0b\x32 .temporal.api.common.v1.Payloads\x12\x10\n\x08identity\x18\x02 \x01(\t"\x99\x01\n\x14\x42\x61tchOperationSignal\x12\x0e\n\x06signal\x18\x01 \x01(\t\x12/\n\x05input\x18\x02 \x01(\x0b\x32 .temporal.api.common.v1.Payloads\x12.\n\x06header\x18\x03 \x01(\x0b\x32\x1e.temporal.api.common.v1.Header\x12\x10\n\x08identity\x18\x04 \x01(\t".\n\x1a\x42\x61tchOperationCancellation\x12\x10\n\x08identity\x18\x01 \x01(\t"*\n\x16\x42\x61tchOperationDeletion\x12\x10\n\x08identity\x18\x01 \x01(\t"\xae\x02\n\x13\x42\x61tchOperationReset\x12\x10\n\x08identity\x18\x03 \x01(\t\x12\x35\n\x07options\x18\x04 \x01(\x0b\x32$.temporal.api.common.v1.ResetOptions\x12\x38\n\nreset_type\x18\x01 \x01(\x0e\x32 .temporal.api.enums.v1.ResetTypeB\x02\x18\x01\x12G\n\x12reset_reapply_type\x18\x02 \x01(\x0e\x32\'.temporal.api.enums.v1.ResetReapplyTypeB\x02\x18\x01\x12K\n\x15post_reset_operations\x18\x05 \x03(\x0b\x32,.temporal.api.workflow.v1.PostResetOperation"\xc9\x01\n,BatchOperationUpdateWorkflowExecutionOptions\x12\x10\n\x08identity\x18\x01 \x01(\t\x12V\n\x1aworkflow_execution_options\x18\x02 \x01(\x0b\x32\x32.temporal.api.workflow.v1.WorkflowExecutionOptions\x12/\n\x0bupdate_mask\x18\x03 \x01(\x0b\x32\x1a.google.protobuf.FieldMask"\xc0\x01\n\x1f\x42\x61tchOperationUnpauseActivities\x12\x10\n\x08identity\x18\x01 \x01(\t\x12\x0e\n\x04type\x18\x02 \x01(\tH\x00\x12\x13\n\tmatch_all\x18\x03 \x01(\x08H\x00\x12\x16\n\x0ereset_attempts\x18\x04 \x01(\x08\x12\x17\n\x0freset_heartbeat\x18\x05 \x01(\x08\x12)\n\x06jitter\x18\x06 \x01(\x0b\x32\x19.google.protobuf.DurationB\n\n\x08\x61\x63tivity"\x84\x01\n!BatchOperationTriggerWorkflowRule\x12\x10\n\x08identity\x18\x01 \x01(\t\x12\x0c\n\x02id\x18\x02 \x01(\tH\x00\x12\x37\n\x04spec\x18\x03 \x01(\x0b\x32\'.temporal.api.rules.v1.WorkflowRuleSpecH\x00\x42\x06\n\x04ruleB\x84\x01\n\x18io.temporal.api.batch.v1B\x0cMessageProtoP\x01Z!go.temporal.io/api/batch/v1;batch\xaa\x02\x17Temporalio.Api.Batch.V1\xea\x02\x1aTemporalio::Api::Batch::V1b\x06proto3'
)


_BATCHOPERATIONINFO = DESCRIPTOR.message_types_by_name["BatchOperationInfo"]
_BATCHOPERATIONTERMINATION = DESCRIPTOR.message_types_by_name[
    "BatchOperationTermination"
]
_BATCHOPERATIONSIGNAL = DESCRIPTOR.message_types_by_name["BatchOperationSignal"]
_BATCHOPERATIONCANCELLATION = DESCRIPTOR.message_types_by_name[
    "BatchOperationCancellation"
]
_BATCHOPERATIONDELETION = DESCRIPTOR.message_types_by_name["BatchOperationDeletion"]
_BATCHOPERATIONRESET = DESCRIPTOR.message_types_by_name["BatchOperationReset"]
_BATCHOPERATIONUPDATEWORKFLOWEXECUTIONOPTIONS = DESCRIPTOR.message_types_by_name[
    "BatchOperationUpdateWorkflowExecutionOptions"
]
_BATCHOPERATIONUNPAUSEACTIVITIES = DESCRIPTOR.message_types_by_name[
    "BatchOperationUnpauseActivities"
]
_BATCHOPERATIONTRIGGERWORKFLOWRULE = DESCRIPTOR.message_types_by_name[
    "BatchOperationTriggerWorkflowRule"
]
BatchOperationInfo = _reflection.GeneratedProtocolMessageType(
    "BatchOperationInfo",
    (_message.Message,),
    {
        "DESCRIPTOR": _BATCHOPERATIONINFO,
        "__module__": "temporal.api.batch.v1.message_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.batch.v1.BatchOperationInfo)
    },
)
_sym_db.RegisterMessage(BatchOperationInfo)

BatchOperationTermination = _reflection.GeneratedProtocolMessageType(
    "BatchOperationTermination",
    (_message.Message,),
    {
        "DESCRIPTOR": _BATCHOPERATIONTERMINATION,
        "__module__": "temporal.api.batch.v1.message_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.batch.v1.BatchOperationTermination)
    },
)
_sym_db.RegisterMessage(BatchOperationTermination)

BatchOperationSignal = _reflection.GeneratedProtocolMessageType(
    "BatchOperationSignal",
    (_message.Message,),
    {
        "DESCRIPTOR": _BATCHOPERATIONSIGNAL,
        "__module__": "temporal.api.batch.v1.message_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.batch.v1.BatchOperationSignal)
    },
)
_sym_db.RegisterMessage(BatchOperationSignal)

BatchOperationCancellation = _reflection.GeneratedProtocolMessageType(
    "BatchOperationCancellation",
    (_message.Message,),
    {
        "DESCRIPTOR": _BATCHOPERATIONCANCELLATION,
        "__module__": "temporal.api.batch.v1.message_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.batch.v1.BatchOperationCancellation)
    },
)
_sym_db.RegisterMessage(BatchOperationCancellation)

BatchOperationDeletion = _reflection.GeneratedProtocolMessageType(
    "BatchOperationDeletion",
    (_message.Message,),
    {
        "DESCRIPTOR": _BATCHOPERATIONDELETION,
        "__module__": "temporal.api.batch.v1.message_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.batch.v1.BatchOperationDeletion)
    },
)
_sym_db.RegisterMessage(BatchOperationDeletion)

BatchOperationReset = _reflection.GeneratedProtocolMessageType(
    "BatchOperationReset",
    (_message.Message,),
    {
        "DESCRIPTOR": _BATCHOPERATIONRESET,
        "__module__": "temporal.api.batch.v1.message_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.batch.v1.BatchOperationReset)
    },
)
_sym_db.RegisterMessage(BatchOperationReset)

BatchOperationUpdateWorkflowExecutionOptions = _reflection.GeneratedProtocolMessageType(
    "BatchOperationUpdateWorkflowExecutionOptions",
    (_message.Message,),
    {
        "DESCRIPTOR": _BATCHOPERATIONUPDATEWORKFLOWEXECUTIONOPTIONS,
        "__module__": "temporal.api.batch.v1.message_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.batch.v1.BatchOperationUpdateWorkflowExecutionOptions)
    },
)
_sym_db.RegisterMessage(BatchOperationUpdateWorkflowExecutionOptions)

BatchOperationUnpauseActivities = _reflection.GeneratedProtocolMessageType(
    "BatchOperationUnpauseActivities",
    (_message.Message,),
    {
        "DESCRIPTOR": _BATCHOPERATIONUNPAUSEACTIVITIES,
        "__module__": "temporal.api.batch.v1.message_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.batch.v1.BatchOperationUnpauseActivities)
    },
)
_sym_db.RegisterMessage(BatchOperationUnpauseActivities)

BatchOperationTriggerWorkflowRule = _reflection.GeneratedProtocolMessageType(
    "BatchOperationTriggerWorkflowRule",
    (_message.Message,),
    {
        "DESCRIPTOR": _BATCHOPERATIONTRIGGERWORKFLOWRULE,
        "__module__": "temporal.api.batch.v1.message_pb2",
        # @@protoc_insertion_point(class_scope:temporal.api.batch.v1.BatchOperationTriggerWorkflowRule)
    },
)
_sym_db.RegisterMessage(BatchOperationTriggerWorkflowRule)

if _descriptor._USE_C_DESCRIPTORS == False:
    DESCRIPTOR._options = None
    DESCRIPTOR._serialized_options = b"\n\030io.temporal.api.batch.v1B\014MessageProtoP\001Z!go.temporal.io/api/batch/v1;batch\252\002\027Temporalio.Api.Batch.V1\352\002\032Temporalio::Api::Batch::V1"
    _BATCHOPERATIONRESET.fields_by_name["reset_type"]._options = None
    _BATCHOPERATIONRESET.fields_by_name["reset_type"]._serialized_options = b"\030\001"
    _BATCHOPERATIONRESET.fields_by_name["reset_reapply_type"]._options = None
    _BATCHOPERATIONRESET.fields_by_name[
        "reset_reapply_type"
    ]._serialized_options = b"\030\001"
    _BATCHOPERATIONINFO._serialized_start = 357
    _BATCHOPERATIONINFO._serialized_end = 548
    _BATCHOPERATIONTERMINATION._serialized_start = 550
    _BATCHOPERATIONTERMINATION._serialized_end = 646
    _BATCHOPERATIONSIGNAL._serialized_start = 649
    _BATCHOPERATIONSIGNAL._serialized_end = 802
    _BATCHOPERATIONCANCELLATION._serialized_start = 804
    _BATCHOPERATIONCANCELLATION._serialized_end = 850
    _BATCHOPERATIONDELETION._serialized_start = 852
    _BATCHOPERATIONDELETION._serialized_end = 894
    _BATCHOPERATIONRESET._serialized_start = 897
    _BATCHOPERATIONRESET._serialized_end = 1199
    _BATCHOPERATIONUPDATEWORKFLOWEXECUTIONOPTIONS._serialized_start = 1202
    _BATCHOPERATIONUPDATEWORKFLOWEXECUTIONOPTIONS._serialized_end = 1403
    _BATCHOPERATIONUNPAUSEACTIVITIES._serialized_start = 1406
    _BATCHOPERATIONUNPAUSEACTIVITIES._serialized_end = 1598
    _BATCHOPERATIONTRIGGERWORKFLOWRULE._serialized_start = 1601
    _BATCHOPERATIONTRIGGERWORKFLOWRULE._serialized_end = 1733
# @@protoc_insertion_point(module_scope)
