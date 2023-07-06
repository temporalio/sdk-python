# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: temporal/sdk/core/workflow_activation/workflow_activation.proto
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

from temporalio.api.common.v1 import (
    message_pb2 as temporal_dot_api_dot_common_dot_v1_dot_message__pb2,
)
from temporalio.api.enums.v1 import (
    workflow_pb2 as temporal_dot_api_dot_enums_dot_v1_dot_workflow__pb2,
)
from temporalio.api.failure.v1 import (
    message_pb2 as temporal_dot_api_dot_failure_dot_v1_dot_message__pb2,
)
from temporalio.bridge.proto.activity_result import (
    activity_result_pb2 as temporal_dot_sdk_dot_core_dot_activity__result_dot_activity__result__pb2,
)
from temporalio.bridge.proto.child_workflow import (
    child_workflow_pb2 as temporal_dot_sdk_dot_core_dot_child__workflow_dot_child__workflow__pb2,
)
from temporalio.bridge.proto.common import (
    common_pb2 as temporal_dot_sdk_dot_core_dot_common_dot_common__pb2,
)

DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(
    b'\n?temporal/sdk/core/workflow_activation/workflow_activation.proto\x12\x1b\x63oresdk.workflow_activation\x1a\x1fgoogle/protobuf/timestamp.proto\x1a\x1egoogle/protobuf/duration.proto\x1a%temporal/api/failure/v1/message.proto\x1a$temporal/api/common/v1/message.proto\x1a$temporal/api/enums/v1/workflow.proto\x1a\x37temporal/sdk/core/activity_result/activity_result.proto\x1a\x35temporal/sdk/core/child_workflow/child_workflow.proto\x1a%temporal/sdk/core/common/common.proto"\xa4\x02\n\x12WorkflowActivation\x12\x0e\n\x06run_id\x18\x01 \x01(\t\x12-\n\ttimestamp\x18\x02 \x01(\x0b\x32\x1a.google.protobuf.Timestamp\x12\x14\n\x0cis_replaying\x18\x03 \x01(\x08\x12\x16\n\x0ehistory_length\x18\x04 \x01(\r\x12@\n\x04jobs\x18\x05 \x03(\x0b\x32\x32.coresdk.workflow_activation.WorkflowActivationJob\x12 \n\x18\x61vailable_internal_flags\x18\x06 \x03(\r\x12\x1a\n\x12history_size_bytes\x18\x07 \x01(\x04\x12!\n\x19\x63ontinue_as_new_suggested\x18\x08 \x01(\x08"\xe1\x08\n\x15WorkflowActivationJob\x12\x44\n\x0estart_workflow\x18\x01 \x01(\x0b\x32*.coresdk.workflow_activation.StartWorkflowH\x00\x12<\n\nfire_timer\x18\x02 \x01(\x0b\x32&.coresdk.workflow_activation.FireTimerH\x00\x12K\n\x12update_random_seed\x18\x04 \x01(\x0b\x32-.coresdk.workflow_activation.UpdateRandomSeedH\x00\x12\x44\n\x0equery_workflow\x18\x05 \x01(\x0b\x32*.coresdk.workflow_activation.QueryWorkflowH\x00\x12\x46\n\x0f\x63\x61ncel_workflow\x18\x06 \x01(\x0b\x32+.coresdk.workflow_activation.CancelWorkflowH\x00\x12\x46\n\x0fsignal_workflow\x18\x07 \x01(\x0b\x32+.coresdk.workflow_activation.SignalWorkflowH\x00\x12H\n\x10resolve_activity\x18\x08 \x01(\x0b\x32,.coresdk.workflow_activation.ResolveActivityH\x00\x12G\n\x10notify_has_patch\x18\t \x01(\x0b\x32+.coresdk.workflow_activation.NotifyHasPatchH\x00\x12q\n&resolve_child_workflow_execution_start\x18\n \x01(\x0b\x32?.coresdk.workflow_activation.ResolveChildWorkflowExecutionStartH\x00\x12\x66\n resolve_child_workflow_execution\x18\x0b \x01(\x0b\x32:.coresdk.workflow_activation.ResolveChildWorkflowExecutionH\x00\x12\x66\n resolve_signal_external_workflow\x18\x0c \x01(\x0b\x32:.coresdk.workflow_activation.ResolveSignalExternalWorkflowH\x00\x12u\n(resolve_request_cancel_external_workflow\x18\r \x01(\x0b\x32\x41.coresdk.workflow_activation.ResolveRequestCancelExternalWorkflowH\x00\x12I\n\x11remove_from_cache\x18\x32 \x01(\x0b\x32,.coresdk.workflow_activation.RemoveFromCacheH\x00\x42\t\n\x07variant"\xd9\t\n\rStartWorkflow\x12\x15\n\rworkflow_type\x18\x01 \x01(\t\x12\x13\n\x0bworkflow_id\x18\x02 \x01(\t\x12\x32\n\targuments\x18\x03 \x03(\x0b\x32\x1f.temporal.api.common.v1.Payload\x12\x17\n\x0frandomness_seed\x18\x04 \x01(\x04\x12H\n\x07headers\x18\x05 \x03(\x0b\x32\x37.coresdk.workflow_activation.StartWorkflow.HeadersEntry\x12\x10\n\x08identity\x18\x06 \x01(\t\x12I\n\x14parent_workflow_info\x18\x07 \x01(\x0b\x32+.coresdk.common.NamespacedWorkflowExecution\x12=\n\x1aworkflow_execution_timeout\x18\x08 \x01(\x0b\x32\x19.google.protobuf.Duration\x12\x37\n\x14workflow_run_timeout\x18\t \x01(\x0b\x32\x19.google.protobuf.Duration\x12\x38\n\x15workflow_task_timeout\x18\n \x01(\x0b\x32\x19.google.protobuf.Duration\x12\'\n\x1f\x63ontinued_from_execution_run_id\x18\x0b \x01(\t\x12J\n\x13\x63ontinued_initiator\x18\x0c \x01(\x0e\x32-.temporal.api.enums.v1.ContinueAsNewInitiator\x12;\n\x11\x63ontinued_failure\x18\r \x01(\x0b\x32 .temporal.api.failure.v1.Failure\x12@\n\x16last_completion_result\x18\x0e \x01(\x0b\x32 .temporal.api.common.v1.Payloads\x12\x1e\n\x16\x66irst_execution_run_id\x18\x0f \x01(\t\x12\x39\n\x0cretry_policy\x18\x10 \x01(\x0b\x32#.temporal.api.common.v1.RetryPolicy\x12\x0f\n\x07\x61ttempt\x18\x11 \x01(\x05\x12\x15\n\rcron_schedule\x18\x12 \x01(\t\x12\x46\n"workflow_execution_expiration_time\x18\x13 \x01(\x0b\x32\x1a.google.protobuf.Timestamp\x12\x45\n"cron_schedule_to_schedule_interval\x18\x14 \x01(\x0b\x32\x19.google.protobuf.Duration\x12*\n\x04memo\x18\x15 \x01(\x0b\x32\x1c.temporal.api.common.v1.Memo\x12\x43\n\x11search_attributes\x18\x16 \x01(\x0b\x32(.temporal.api.common.v1.SearchAttributes\x12.\n\nstart_time\x18\x17 \x01(\x0b\x32\x1a.google.protobuf.Timestamp\x1aO\n\x0cHeadersEntry\x12\x0b\n\x03key\x18\x01 \x01(\t\x12.\n\x05value\x18\x02 \x01(\x0b\x32\x1f.temporal.api.common.v1.Payload:\x02\x38\x01"\x18\n\tFireTimer\x12\x0b\n\x03seq\x18\x01 \x01(\r"[\n\x0fResolveActivity\x12\x0b\n\x03seq\x18\x01 \x01(\r\x12;\n\x06result\x18\x02 \x01(\x0b\x32+.coresdk.activity_result.ActivityResolution"\xd1\x02\n"ResolveChildWorkflowExecutionStart\x12\x0b\n\x03seq\x18\x01 \x01(\r\x12[\n\tsucceeded\x18\x02 \x01(\x0b\x32\x46.coresdk.workflow_activation.ResolveChildWorkflowExecutionStartSuccessH\x00\x12X\n\x06\x66\x61iled\x18\x03 \x01(\x0b\x32\x46.coresdk.workflow_activation.ResolveChildWorkflowExecutionStartFailureH\x00\x12]\n\tcancelled\x18\x04 \x01(\x0b\x32H.coresdk.workflow_activation.ResolveChildWorkflowExecutionStartCancelledH\x00\x42\x08\n\x06status";\n)ResolveChildWorkflowExecutionStartSuccess\x12\x0e\n\x06run_id\x18\x01 \x01(\t"\xa6\x01\n)ResolveChildWorkflowExecutionStartFailure\x12\x13\n\x0bworkflow_id\x18\x01 \x01(\t\x12\x15\n\rworkflow_type\x18\x02 \x01(\t\x12M\n\x05\x63\x61use\x18\x03 \x01(\x0e\x32>.coresdk.child_workflow.StartChildWorkflowExecutionFailedCause"`\n+ResolveChildWorkflowExecutionStartCancelled\x12\x31\n\x07\x66\x61ilure\x18\x01 \x01(\x0b\x32 .temporal.api.failure.v1.Failure"i\n\x1dResolveChildWorkflowExecution\x12\x0b\n\x03seq\x18\x01 \x01(\r\x12;\n\x06result\x18\x02 \x01(\x0b\x32+.coresdk.child_workflow.ChildWorkflowResult"+\n\x10UpdateRandomSeed\x12\x17\n\x0frandomness_seed\x18\x01 \x01(\x04"\x84\x02\n\rQueryWorkflow\x12\x10\n\x08query_id\x18\x01 \x01(\t\x12\x12\n\nquery_type\x18\x02 \x01(\t\x12\x32\n\targuments\x18\x03 \x03(\x0b\x32\x1f.temporal.api.common.v1.Payload\x12H\n\x07headers\x18\x05 \x03(\x0b\x32\x37.coresdk.workflow_activation.QueryWorkflow.HeadersEntry\x1aO\n\x0cHeadersEntry\x12\x0b\n\x03key\x18\x01 \x01(\t\x12.\n\x05value\x18\x02 \x01(\x0b\x32\x1f.temporal.api.common.v1.Payload:\x02\x38\x01"B\n\x0e\x43\x61ncelWorkflow\x12\x30\n\x07\x64\x65tails\x18\x01 \x03(\x0b\x32\x1f.temporal.api.common.v1.Payload"\x83\x02\n\x0eSignalWorkflow\x12\x13\n\x0bsignal_name\x18\x01 \x01(\t\x12.\n\x05input\x18\x02 \x03(\x0b\x32\x1f.temporal.api.common.v1.Payload\x12\x10\n\x08identity\x18\x03 \x01(\t\x12I\n\x07headers\x18\x05 \x03(\x0b\x32\x38.coresdk.workflow_activation.SignalWorkflow.HeadersEntry\x1aO\n\x0cHeadersEntry\x12\x0b\n\x03key\x18\x01 \x01(\t\x12.\n\x05value\x18\x02 \x01(\x0b\x32\x1f.temporal.api.common.v1.Payload:\x02\x38\x01""\n\x0eNotifyHasPatch\x12\x10\n\x08patch_id\x18\x01 \x01(\t"_\n\x1dResolveSignalExternalWorkflow\x12\x0b\n\x03seq\x18\x01 \x01(\r\x12\x31\n\x07\x66\x61ilure\x18\x02 \x01(\x0b\x32 .temporal.api.failure.v1.Failure"f\n$ResolveRequestCancelExternalWorkflow\x12\x0b\n\x03seq\x18\x01 \x01(\r\x12\x31\n\x07\x66\x61ilure\x18\x02 \x01(\x0b\x32 .temporal.api.failure.v1.Failure"\xc1\x02\n\x0fRemoveFromCache\x12\x0f\n\x07message\x18\x01 \x01(\t\x12K\n\x06reason\x18\x02 \x01(\x0e\x32;.coresdk.workflow_activation.RemoveFromCache.EvictionReason"\xcf\x01\n\x0e\x45victionReason\x12\x0f\n\x0bUNSPECIFIED\x10\x00\x12\x0e\n\nCACHE_FULL\x10\x01\x12\x0e\n\nCACHE_MISS\x10\x02\x12\x12\n\x0eNONDETERMINISM\x10\x03\x12\r\n\tLANG_FAIL\x10\x04\x12\x12\n\x0eLANG_REQUESTED\x10\x05\x12\x12\n\x0eTASK_NOT_FOUND\x10\x06\x12\x15\n\x11UNHANDLED_COMMAND\x10\x07\x12\t\n\x05\x46\x41TAL\x10\x08\x12\x1f\n\x1bPAGINATION_OR_HISTORY_FETCH\x10\tB.\xea\x02+Temporalio::Bridge::Api::WorkflowActivationb\x06proto3'
)


_WORKFLOWACTIVATION = DESCRIPTOR.message_types_by_name["WorkflowActivation"]
_WORKFLOWACTIVATIONJOB = DESCRIPTOR.message_types_by_name["WorkflowActivationJob"]
_STARTWORKFLOW = DESCRIPTOR.message_types_by_name["StartWorkflow"]
_STARTWORKFLOW_HEADERSENTRY = _STARTWORKFLOW.nested_types_by_name["HeadersEntry"]
_FIRETIMER = DESCRIPTOR.message_types_by_name["FireTimer"]
_RESOLVEACTIVITY = DESCRIPTOR.message_types_by_name["ResolveActivity"]
_RESOLVECHILDWORKFLOWEXECUTIONSTART = DESCRIPTOR.message_types_by_name[
    "ResolveChildWorkflowExecutionStart"
]
_RESOLVECHILDWORKFLOWEXECUTIONSTARTSUCCESS = DESCRIPTOR.message_types_by_name[
    "ResolveChildWorkflowExecutionStartSuccess"
]
_RESOLVECHILDWORKFLOWEXECUTIONSTARTFAILURE = DESCRIPTOR.message_types_by_name[
    "ResolveChildWorkflowExecutionStartFailure"
]
_RESOLVECHILDWORKFLOWEXECUTIONSTARTCANCELLED = DESCRIPTOR.message_types_by_name[
    "ResolveChildWorkflowExecutionStartCancelled"
]
_RESOLVECHILDWORKFLOWEXECUTION = DESCRIPTOR.message_types_by_name[
    "ResolveChildWorkflowExecution"
]
_UPDATERANDOMSEED = DESCRIPTOR.message_types_by_name["UpdateRandomSeed"]
_QUERYWORKFLOW = DESCRIPTOR.message_types_by_name["QueryWorkflow"]
_QUERYWORKFLOW_HEADERSENTRY = _QUERYWORKFLOW.nested_types_by_name["HeadersEntry"]
_CANCELWORKFLOW = DESCRIPTOR.message_types_by_name["CancelWorkflow"]
_SIGNALWORKFLOW = DESCRIPTOR.message_types_by_name["SignalWorkflow"]
_SIGNALWORKFLOW_HEADERSENTRY = _SIGNALWORKFLOW.nested_types_by_name["HeadersEntry"]
_NOTIFYHASPATCH = DESCRIPTOR.message_types_by_name["NotifyHasPatch"]
_RESOLVESIGNALEXTERNALWORKFLOW = DESCRIPTOR.message_types_by_name[
    "ResolveSignalExternalWorkflow"
]
_RESOLVEREQUESTCANCELEXTERNALWORKFLOW = DESCRIPTOR.message_types_by_name[
    "ResolveRequestCancelExternalWorkflow"
]
_REMOVEFROMCACHE = DESCRIPTOR.message_types_by_name["RemoveFromCache"]
_REMOVEFROMCACHE_EVICTIONREASON = _REMOVEFROMCACHE.enum_types_by_name["EvictionReason"]
WorkflowActivation = _reflection.GeneratedProtocolMessageType(
    "WorkflowActivation",
    (_message.Message,),
    {
        "DESCRIPTOR": _WORKFLOWACTIVATION,
        "__module__": "temporal.sdk.core.workflow_activation.workflow_activation_pb2"
        # @@protoc_insertion_point(class_scope:coresdk.workflow_activation.WorkflowActivation)
    },
)
_sym_db.RegisterMessage(WorkflowActivation)

WorkflowActivationJob = _reflection.GeneratedProtocolMessageType(
    "WorkflowActivationJob",
    (_message.Message,),
    {
        "DESCRIPTOR": _WORKFLOWACTIVATIONJOB,
        "__module__": "temporal.sdk.core.workflow_activation.workflow_activation_pb2"
        # @@protoc_insertion_point(class_scope:coresdk.workflow_activation.WorkflowActivationJob)
    },
)
_sym_db.RegisterMessage(WorkflowActivationJob)

StartWorkflow = _reflection.GeneratedProtocolMessageType(
    "StartWorkflow",
    (_message.Message,),
    {
        "HeadersEntry": _reflection.GeneratedProtocolMessageType(
            "HeadersEntry",
            (_message.Message,),
            {
                "DESCRIPTOR": _STARTWORKFLOW_HEADERSENTRY,
                "__module__": "temporal.sdk.core.workflow_activation.workflow_activation_pb2"
                # @@protoc_insertion_point(class_scope:coresdk.workflow_activation.StartWorkflow.HeadersEntry)
            },
        ),
        "DESCRIPTOR": _STARTWORKFLOW,
        "__module__": "temporal.sdk.core.workflow_activation.workflow_activation_pb2"
        # @@protoc_insertion_point(class_scope:coresdk.workflow_activation.StartWorkflow)
    },
)
_sym_db.RegisterMessage(StartWorkflow)
_sym_db.RegisterMessage(StartWorkflow.HeadersEntry)

FireTimer = _reflection.GeneratedProtocolMessageType(
    "FireTimer",
    (_message.Message,),
    {
        "DESCRIPTOR": _FIRETIMER,
        "__module__": "temporal.sdk.core.workflow_activation.workflow_activation_pb2"
        # @@protoc_insertion_point(class_scope:coresdk.workflow_activation.FireTimer)
    },
)
_sym_db.RegisterMessage(FireTimer)

ResolveActivity = _reflection.GeneratedProtocolMessageType(
    "ResolveActivity",
    (_message.Message,),
    {
        "DESCRIPTOR": _RESOLVEACTIVITY,
        "__module__": "temporal.sdk.core.workflow_activation.workflow_activation_pb2"
        # @@protoc_insertion_point(class_scope:coresdk.workflow_activation.ResolveActivity)
    },
)
_sym_db.RegisterMessage(ResolveActivity)

ResolveChildWorkflowExecutionStart = _reflection.GeneratedProtocolMessageType(
    "ResolveChildWorkflowExecutionStart",
    (_message.Message,),
    {
        "DESCRIPTOR": _RESOLVECHILDWORKFLOWEXECUTIONSTART,
        "__module__": "temporal.sdk.core.workflow_activation.workflow_activation_pb2"
        # @@protoc_insertion_point(class_scope:coresdk.workflow_activation.ResolveChildWorkflowExecutionStart)
    },
)
_sym_db.RegisterMessage(ResolveChildWorkflowExecutionStart)

ResolveChildWorkflowExecutionStartSuccess = _reflection.GeneratedProtocolMessageType(
    "ResolveChildWorkflowExecutionStartSuccess",
    (_message.Message,),
    {
        "DESCRIPTOR": _RESOLVECHILDWORKFLOWEXECUTIONSTARTSUCCESS,
        "__module__": "temporal.sdk.core.workflow_activation.workflow_activation_pb2"
        # @@protoc_insertion_point(class_scope:coresdk.workflow_activation.ResolveChildWorkflowExecutionStartSuccess)
    },
)
_sym_db.RegisterMessage(ResolveChildWorkflowExecutionStartSuccess)

ResolveChildWorkflowExecutionStartFailure = _reflection.GeneratedProtocolMessageType(
    "ResolveChildWorkflowExecutionStartFailure",
    (_message.Message,),
    {
        "DESCRIPTOR": _RESOLVECHILDWORKFLOWEXECUTIONSTARTFAILURE,
        "__module__": "temporal.sdk.core.workflow_activation.workflow_activation_pb2"
        # @@protoc_insertion_point(class_scope:coresdk.workflow_activation.ResolveChildWorkflowExecutionStartFailure)
    },
)
_sym_db.RegisterMessage(ResolveChildWorkflowExecutionStartFailure)

ResolveChildWorkflowExecutionStartCancelled = _reflection.GeneratedProtocolMessageType(
    "ResolveChildWorkflowExecutionStartCancelled",
    (_message.Message,),
    {
        "DESCRIPTOR": _RESOLVECHILDWORKFLOWEXECUTIONSTARTCANCELLED,
        "__module__": "temporal.sdk.core.workflow_activation.workflow_activation_pb2"
        # @@protoc_insertion_point(class_scope:coresdk.workflow_activation.ResolveChildWorkflowExecutionStartCancelled)
    },
)
_sym_db.RegisterMessage(ResolveChildWorkflowExecutionStartCancelled)

ResolveChildWorkflowExecution = _reflection.GeneratedProtocolMessageType(
    "ResolveChildWorkflowExecution",
    (_message.Message,),
    {
        "DESCRIPTOR": _RESOLVECHILDWORKFLOWEXECUTION,
        "__module__": "temporal.sdk.core.workflow_activation.workflow_activation_pb2"
        # @@protoc_insertion_point(class_scope:coresdk.workflow_activation.ResolveChildWorkflowExecution)
    },
)
_sym_db.RegisterMessage(ResolveChildWorkflowExecution)

UpdateRandomSeed = _reflection.GeneratedProtocolMessageType(
    "UpdateRandomSeed",
    (_message.Message,),
    {
        "DESCRIPTOR": _UPDATERANDOMSEED,
        "__module__": "temporal.sdk.core.workflow_activation.workflow_activation_pb2"
        # @@protoc_insertion_point(class_scope:coresdk.workflow_activation.UpdateRandomSeed)
    },
)
_sym_db.RegisterMessage(UpdateRandomSeed)

QueryWorkflow = _reflection.GeneratedProtocolMessageType(
    "QueryWorkflow",
    (_message.Message,),
    {
        "HeadersEntry": _reflection.GeneratedProtocolMessageType(
            "HeadersEntry",
            (_message.Message,),
            {
                "DESCRIPTOR": _QUERYWORKFLOW_HEADERSENTRY,
                "__module__": "temporal.sdk.core.workflow_activation.workflow_activation_pb2"
                # @@protoc_insertion_point(class_scope:coresdk.workflow_activation.QueryWorkflow.HeadersEntry)
            },
        ),
        "DESCRIPTOR": _QUERYWORKFLOW,
        "__module__": "temporal.sdk.core.workflow_activation.workflow_activation_pb2"
        # @@protoc_insertion_point(class_scope:coresdk.workflow_activation.QueryWorkflow)
    },
)
_sym_db.RegisterMessage(QueryWorkflow)
_sym_db.RegisterMessage(QueryWorkflow.HeadersEntry)

CancelWorkflow = _reflection.GeneratedProtocolMessageType(
    "CancelWorkflow",
    (_message.Message,),
    {
        "DESCRIPTOR": _CANCELWORKFLOW,
        "__module__": "temporal.sdk.core.workflow_activation.workflow_activation_pb2"
        # @@protoc_insertion_point(class_scope:coresdk.workflow_activation.CancelWorkflow)
    },
)
_sym_db.RegisterMessage(CancelWorkflow)

SignalWorkflow = _reflection.GeneratedProtocolMessageType(
    "SignalWorkflow",
    (_message.Message,),
    {
        "HeadersEntry": _reflection.GeneratedProtocolMessageType(
            "HeadersEntry",
            (_message.Message,),
            {
                "DESCRIPTOR": _SIGNALWORKFLOW_HEADERSENTRY,
                "__module__": "temporal.sdk.core.workflow_activation.workflow_activation_pb2"
                # @@protoc_insertion_point(class_scope:coresdk.workflow_activation.SignalWorkflow.HeadersEntry)
            },
        ),
        "DESCRIPTOR": _SIGNALWORKFLOW,
        "__module__": "temporal.sdk.core.workflow_activation.workflow_activation_pb2"
        # @@protoc_insertion_point(class_scope:coresdk.workflow_activation.SignalWorkflow)
    },
)
_sym_db.RegisterMessage(SignalWorkflow)
_sym_db.RegisterMessage(SignalWorkflow.HeadersEntry)

NotifyHasPatch = _reflection.GeneratedProtocolMessageType(
    "NotifyHasPatch",
    (_message.Message,),
    {
        "DESCRIPTOR": _NOTIFYHASPATCH,
        "__module__": "temporal.sdk.core.workflow_activation.workflow_activation_pb2"
        # @@protoc_insertion_point(class_scope:coresdk.workflow_activation.NotifyHasPatch)
    },
)
_sym_db.RegisterMessage(NotifyHasPatch)

ResolveSignalExternalWorkflow = _reflection.GeneratedProtocolMessageType(
    "ResolveSignalExternalWorkflow",
    (_message.Message,),
    {
        "DESCRIPTOR": _RESOLVESIGNALEXTERNALWORKFLOW,
        "__module__": "temporal.sdk.core.workflow_activation.workflow_activation_pb2"
        # @@protoc_insertion_point(class_scope:coresdk.workflow_activation.ResolveSignalExternalWorkflow)
    },
)
_sym_db.RegisterMessage(ResolveSignalExternalWorkflow)

ResolveRequestCancelExternalWorkflow = _reflection.GeneratedProtocolMessageType(
    "ResolveRequestCancelExternalWorkflow",
    (_message.Message,),
    {
        "DESCRIPTOR": _RESOLVEREQUESTCANCELEXTERNALWORKFLOW,
        "__module__": "temporal.sdk.core.workflow_activation.workflow_activation_pb2"
        # @@protoc_insertion_point(class_scope:coresdk.workflow_activation.ResolveRequestCancelExternalWorkflow)
    },
)
_sym_db.RegisterMessage(ResolveRequestCancelExternalWorkflow)

RemoveFromCache = _reflection.GeneratedProtocolMessageType(
    "RemoveFromCache",
    (_message.Message,),
    {
        "DESCRIPTOR": _REMOVEFROMCACHE,
        "__module__": "temporal.sdk.core.workflow_activation.workflow_activation_pb2"
        # @@protoc_insertion_point(class_scope:coresdk.workflow_activation.RemoveFromCache)
    },
)
_sym_db.RegisterMessage(RemoveFromCache)

if _descriptor._USE_C_DESCRIPTORS == False:
    DESCRIPTOR._options = None
    DESCRIPTOR._serialized_options = (
        b"\352\002+Temporalio::Bridge::Api::WorkflowActivation"
    )
    _STARTWORKFLOW_HEADERSENTRY._options = None
    _STARTWORKFLOW_HEADERSENTRY._serialized_options = b"8\001"
    _QUERYWORKFLOW_HEADERSENTRY._options = None
    _QUERYWORKFLOW_HEADERSENTRY._serialized_options = b"8\001"
    _SIGNALWORKFLOW_HEADERSENTRY._options = None
    _SIGNALWORKFLOW_HEADERSENTRY._serialized_options = b"8\001"
    _WORKFLOWACTIVATION._serialized_start = 428
    _WORKFLOWACTIVATION._serialized_end = 720
    _WORKFLOWACTIVATIONJOB._serialized_start = 723
    _WORKFLOWACTIVATIONJOB._serialized_end = 1844
    _STARTWORKFLOW._serialized_start = 1847
    _STARTWORKFLOW._serialized_end = 3088
    _STARTWORKFLOW_HEADERSENTRY._serialized_start = 3009
    _STARTWORKFLOW_HEADERSENTRY._serialized_end = 3088
    _FIRETIMER._serialized_start = 3090
    _FIRETIMER._serialized_end = 3114
    _RESOLVEACTIVITY._serialized_start = 3116
    _RESOLVEACTIVITY._serialized_end = 3207
    _RESOLVECHILDWORKFLOWEXECUTIONSTART._serialized_start = 3210
    _RESOLVECHILDWORKFLOWEXECUTIONSTART._serialized_end = 3547
    _RESOLVECHILDWORKFLOWEXECUTIONSTARTSUCCESS._serialized_start = 3549
    _RESOLVECHILDWORKFLOWEXECUTIONSTARTSUCCESS._serialized_end = 3608
    _RESOLVECHILDWORKFLOWEXECUTIONSTARTFAILURE._serialized_start = 3611
    _RESOLVECHILDWORKFLOWEXECUTIONSTARTFAILURE._serialized_end = 3777
    _RESOLVECHILDWORKFLOWEXECUTIONSTARTCANCELLED._serialized_start = 3779
    _RESOLVECHILDWORKFLOWEXECUTIONSTARTCANCELLED._serialized_end = 3875
    _RESOLVECHILDWORKFLOWEXECUTION._serialized_start = 3877
    _RESOLVECHILDWORKFLOWEXECUTION._serialized_end = 3982
    _UPDATERANDOMSEED._serialized_start = 3984
    _UPDATERANDOMSEED._serialized_end = 4027
    _QUERYWORKFLOW._serialized_start = 4030
    _QUERYWORKFLOW._serialized_end = 4290
    _QUERYWORKFLOW_HEADERSENTRY._serialized_start = 3009
    _QUERYWORKFLOW_HEADERSENTRY._serialized_end = 3088
    _CANCELWORKFLOW._serialized_start = 4292
    _CANCELWORKFLOW._serialized_end = 4358
    _SIGNALWORKFLOW._serialized_start = 4361
    _SIGNALWORKFLOW._serialized_end = 4620
    _SIGNALWORKFLOW_HEADERSENTRY._serialized_start = 3009
    _SIGNALWORKFLOW_HEADERSENTRY._serialized_end = 3088
    _NOTIFYHASPATCH._serialized_start = 4622
    _NOTIFYHASPATCH._serialized_end = 4656
    _RESOLVESIGNALEXTERNALWORKFLOW._serialized_start = 4658
    _RESOLVESIGNALEXTERNALWORKFLOW._serialized_end = 4753
    _RESOLVEREQUESTCANCELEXTERNALWORKFLOW._serialized_start = 4755
    _RESOLVEREQUESTCANCELEXTERNALWORKFLOW._serialized_end = 4857
    _REMOVEFROMCACHE._serialized_start = 4860
    _REMOVEFROMCACHE._serialized_end = 5181
    _REMOVEFROMCACHE_EVICTIONREASON._serialized_start = 4974
    _REMOVEFROMCACHE_EVICTIONREASON._serialized_end = 5181
# @@protoc_insertion_point(module_scope)
