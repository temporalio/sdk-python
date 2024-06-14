# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: temporal/api/workflowservice/v1/service.proto
"""Generated protocol buffer code."""
from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection
from google.protobuf import symbol_database as _symbol_database

# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()


from google.api import annotations_pb2 as google_dot_api_dot_annotations__pb2

from temporalio.api.workflowservice.v1 import (
    request_response_pb2 as temporal_dot_api_dot_workflowservice_dot_v1_dot_request__response__pb2,
)

DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(
    b'\n-temporal/api/workflowservice/v1/service.proto\x12\x1ftemporal.api.workflowservice.v1\x1a\x36temporal/api/workflowservice/v1/request_response.proto\x1a\x1cgoogle/api/annotations.proto2\xbb\x63\n\x0fWorkflowService\x12\xa2\x01\n\x11RegisterNamespace\x12\x39.temporal.api.workflowservice.v1.RegisterNamespaceRequest\x1a:.temporal.api.workflowservice.v1.RegisterNamespaceResponse"\x16\x82\xd3\xe4\x93\x02\x10"\x0b/namespaces:\x01*\x12\xab\x01\n\x11\x44\x65scribeNamespace\x12\x39.temporal.api.workflowservice.v1.DescribeNamespaceRequest\x1a:.temporal.api.workflowservice.v1.DescribeNamespaceResponse"\x1f\x82\xd3\xe4\x93\x02\x19\x12\x17/namespaces/{namespace}\x12\x96\x01\n\x0eListNamespaces\x12\x36.temporal.api.workflowservice.v1.ListNamespacesRequest\x1a\x37.temporal.api.workflowservice.v1.ListNamespacesResponse"\x13\x82\xd3\xe4\x93\x02\r\x12\x0b/namespaces\x12\xaf\x01\n\x0fUpdateNamespace\x12\x37.temporal.api.workflowservice.v1.UpdateNamespaceRequest\x1a\x38.temporal.api.workflowservice.v1.UpdateNamespaceResponse")\x82\xd3\xe4\x93\x02#"\x1e/namespaces/{namespace}/update:\x01*\x12\x8f\x01\n\x12\x44\x65precateNamespace\x12:.temporal.api.workflowservice.v1.DeprecateNamespaceRequest\x1a;.temporal.api.workflowservice.v1.DeprecateNamespaceResponse"\x00\x12\xd5\x01\n\x16StartWorkflowExecution\x12>.temporal.api.workflowservice.v1.StartWorkflowExecutionRequest\x1a?.temporal.api.workflowservice.v1.StartWorkflowExecutionResponse":\x82\xd3\xe4\x93\x02\x34"//namespaces/{namespace}/workflows/{workflow_id}:\x01*\x12\xdc\x01\n\x15\x45xecuteMultiOperation\x12=.temporal.api.workflowservice.v1.ExecuteMultiOperationRequest\x1a>.temporal.api.workflowservice.v1.ExecuteMultiOperationResponse"D\x82\xd3\xe4\x93\x02>"9/namespaces/{namespace}/workflows/execute-multi-operation:\x01*\x12\xf3\x01\n\x1bGetWorkflowExecutionHistory\x12\x43.temporal.api.workflowservice.v1.GetWorkflowExecutionHistoryRequest\x1a\x44.temporal.api.workflowservice.v1.GetWorkflowExecutionHistoryResponse"I\x82\xd3\xe4\x93\x02\x43\x12\x41/namespaces/{namespace}/workflows/{execution.workflow_id}/history\x12\x90\x02\n"GetWorkflowExecutionHistoryReverse\x12J.temporal.api.workflowservice.v1.GetWorkflowExecutionHistoryReverseRequest\x1aK.temporal.api.workflowservice.v1.GetWorkflowExecutionHistoryReverseResponse"Q\x82\xd3\xe4\x93\x02K\x12I/namespaces/{namespace}/workflows/{execution.workflow_id}/history-reverse\x12\x98\x01\n\x15PollWorkflowTaskQueue\x12=.temporal.api.workflowservice.v1.PollWorkflowTaskQueueRequest\x1a>.temporal.api.workflowservice.v1.PollWorkflowTaskQueueResponse"\x00\x12\xad\x01\n\x1cRespondWorkflowTaskCompleted\x12\x44.temporal.api.workflowservice.v1.RespondWorkflowTaskCompletedRequest\x1a\x45.temporal.api.workflowservice.v1.RespondWorkflowTaskCompletedResponse"\x00\x12\xa4\x01\n\x19RespondWorkflowTaskFailed\x12\x41.temporal.api.workflowservice.v1.RespondWorkflowTaskFailedRequest\x1a\x42.temporal.api.workflowservice.v1.RespondWorkflowTaskFailedResponse"\x00\x12\x98\x01\n\x15PollActivityTaskQueue\x12=.temporal.api.workflowservice.v1.PollActivityTaskQueueRequest\x1a>.temporal.api.workflowservice.v1.PollActivityTaskQueueResponse"\x00\x12\xe1\x01\n\x1bRecordActivityTaskHeartbeat\x12\x43.temporal.api.workflowservice.v1.RecordActivityTaskHeartbeatRequest\x1a\x44.temporal.api.workflowservice.v1.RecordActivityTaskHeartbeatResponse"7\x82\xd3\xe4\x93\x02\x31",/namespaces/{namespace}/activities/heartbeat:\x01*\x12\xf3\x01\n\x1fRecordActivityTaskHeartbeatById\x12G.temporal.api.workflowservice.v1.RecordActivityTaskHeartbeatByIdRequest\x1aH.temporal.api.workflowservice.v1.RecordActivityTaskHeartbeatByIdResponse"=\x82\xd3\xe4\x93\x02\x37"2/namespaces/{namespace}/activities/heartbeat-by-id:\x01*\x12\xe3\x01\n\x1cRespondActivityTaskCompleted\x12\x44.temporal.api.workflowservice.v1.RespondActivityTaskCompletedRequest\x1a\x45.temporal.api.workflowservice.v1.RespondActivityTaskCompletedResponse"6\x82\xd3\xe4\x93\x02\x30"+/namespaces/{namespace}/activities/complete:\x01*\x12\xf5\x01\n RespondActivityTaskCompletedById\x12H.temporal.api.workflowservice.v1.RespondActivityTaskCompletedByIdRequest\x1aI.temporal.api.workflowservice.v1.RespondActivityTaskCompletedByIdResponse"<\x82\xd3\xe4\x93\x02\x36"1/namespaces/{namespace}/activities/complete-by-id:\x01*\x12\xd6\x01\n\x19RespondActivityTaskFailed\x12\x41.temporal.api.workflowservice.v1.RespondActivityTaskFailedRequest\x1a\x42.temporal.api.workflowservice.v1.RespondActivityTaskFailedResponse"2\x82\xd3\xe4\x93\x02,"\'/namespaces/{namespace}/activities/fail:\x01*\x12\xe8\x01\n\x1dRespondActivityTaskFailedById\x12\x45.temporal.api.workflowservice.v1.RespondActivityTaskFailedByIdRequest\x1a\x46.temporal.api.workflowservice.v1.RespondActivityTaskFailedByIdResponse"8\x82\xd3\xe4\x93\x02\x32"-/namespaces/{namespace}/activities/fail-by-id:\x01*\x12\xde\x01\n\x1bRespondActivityTaskCanceled\x12\x43.temporal.api.workflowservice.v1.RespondActivityTaskCanceledRequest\x1a\x44.temporal.api.workflowservice.v1.RespondActivityTaskCanceledResponse"4\x82\xd3\xe4\x93\x02.")/namespaces/{namespace}/activities/cancel:\x01*\x12\xf0\x01\n\x1fRespondActivityTaskCanceledById\x12G.temporal.api.workflowservice.v1.RespondActivityTaskCanceledByIdRequest\x1aH.temporal.api.workflowservice.v1.RespondActivityTaskCanceledByIdResponse":\x82\xd3\xe4\x93\x02\x34"//namespaces/{namespace}/activities/cancel-by-id:\x01*\x12\x87\x02\n\x1eRequestCancelWorkflowExecution\x12\x46.temporal.api.workflowservice.v1.RequestCancelWorkflowExecutionRequest\x1aG.temporal.api.workflowservice.v1.RequestCancelWorkflowExecutionResponse"T\x82\xd3\xe4\x93\x02N"I/namespaces/{namespace}/workflows/{workflow_execution.workflow_id}/cancel:\x01*\x12\x80\x02\n\x17SignalWorkflowExecution\x12?.temporal.api.workflowservice.v1.SignalWorkflowExecutionRequest\x1a@.temporal.api.workflowservice.v1.SignalWorkflowExecutionResponse"b\x82\xd3\xe4\x93\x02\\"W/namespaces/{namespace}/workflows/{workflow_execution.workflow_id}/signal/{signal_name}:\x01*\x12\x93\x02\n SignalWithStartWorkflowExecution\x12H.temporal.api.workflowservice.v1.SignalWithStartWorkflowExecutionRequest\x1aI.temporal.api.workflowservice.v1.SignalWithStartWorkflowExecutionResponse"Z\x82\xd3\xe4\x93\x02T"O/namespaces/{namespace}/workflows/{workflow_id}/signal-with-start/{signal_name}:\x01*\x12\xee\x01\n\x16ResetWorkflowExecution\x12>.temporal.api.workflowservice.v1.ResetWorkflowExecutionRequest\x1a?.temporal.api.workflowservice.v1.ResetWorkflowExecutionResponse"S\x82\xd3\xe4\x93\x02M"H/namespaces/{namespace}/workflows/{workflow_execution.workflow_id}/reset:\x01*\x12\xfe\x01\n\x1aTerminateWorkflowExecution\x12\x42.temporal.api.workflowservice.v1.TerminateWorkflowExecutionRequest\x1a\x43.temporal.api.workflowservice.v1.TerminateWorkflowExecutionResponse"W\x82\xd3\xe4\x93\x02Q"L/namespaces/{namespace}/workflows/{workflow_execution.workflow_id}/terminate:\x01*\x12\x9e\x01\n\x17\x44\x65leteWorkflowExecution\x12?.temporal.api.workflowservice.v1.DeleteWorkflowExecutionRequest\x1a@.temporal.api.workflowservice.v1.DeleteWorkflowExecutionResponse"\x00\x12\xa7\x01\n\x1aListOpenWorkflowExecutions\x12\x42.temporal.api.workflowservice.v1.ListOpenWorkflowExecutionsRequest\x1a\x43.temporal.api.workflowservice.v1.ListOpenWorkflowExecutionsResponse"\x00\x12\xad\x01\n\x1cListClosedWorkflowExecutions\x12\x44.temporal.api.workflowservice.v1.ListClosedWorkflowExecutionsRequest\x1a\x45.temporal.api.workflowservice.v1.ListClosedWorkflowExecutionsResponse"\x00\x12\xc4\x01\n\x16ListWorkflowExecutions\x12>.temporal.api.workflowservice.v1.ListWorkflowExecutionsRequest\x1a?.temporal.api.workflowservice.v1.ListWorkflowExecutionsResponse")\x82\xd3\xe4\x93\x02#\x12!/namespaces/{namespace}/workflows\x12\xe5\x01\n\x1eListArchivedWorkflowExecutions\x12\x46.temporal.api.workflowservice.v1.ListArchivedWorkflowExecutionsRequest\x1aG.temporal.api.workflowservice.v1.ListArchivedWorkflowExecutionsResponse"2\x82\xd3\xe4\x93\x02,\x12*/namespaces/{namespace}/archived-workflows\x12\x9b\x01\n\x16ScanWorkflowExecutions\x12>.temporal.api.workflowservice.v1.ScanWorkflowExecutionsRequest\x1a?.temporal.api.workflowservice.v1.ScanWorkflowExecutionsResponse"\x00\x12\xcc\x01\n\x17\x43ountWorkflowExecutions\x12?.temporal.api.workflowservice.v1.CountWorkflowExecutionsRequest\x1a@.temporal.api.workflowservice.v1.CountWorkflowExecutionsResponse".\x82\xd3\xe4\x93\x02(\x12&/namespaces/{namespace}/workflow-count\x12\x92\x01\n\x13GetSearchAttributes\x12;.temporal.api.workflowservice.v1.GetSearchAttributesRequest\x1a<.temporal.api.workflowservice.v1.GetSearchAttributesResponse"\x00\x12\xa4\x01\n\x19RespondQueryTaskCompleted\x12\x41.temporal.api.workflowservice.v1.RespondQueryTaskCompletedRequest\x1a\x42.temporal.api.workflowservice.v1.RespondQueryTaskCompletedResponse"\x00\x12\x95\x01\n\x14ResetStickyTaskQueue\x12<.temporal.api.workflowservice.v1.ResetStickyTaskQueueRequest\x1a=.temporal.api.workflowservice.v1.ResetStickyTaskQueueResponse"\x00\x12\xdd\x01\n\rQueryWorkflow\x12\x35.temporal.api.workflowservice.v1.QueryWorkflowRequest\x1a\x36.temporal.api.workflowservice.v1.QueryWorkflowResponse"]\x82\xd3\xe4\x93\x02W"R/namespaces/{namespace}/workflows/{execution.workflow_id}/query/{query.query_type}:\x01*\x12\xe5\x01\n\x19\x44\x65scribeWorkflowExecution\x12\x41.temporal.api.workflowservice.v1.DescribeWorkflowExecutionRequest\x1a\x42.temporal.api.workflowservice.v1.DescribeWorkflowExecutionResponse"A\x82\xd3\xe4\x93\x02;\x12\x39/namespaces/{namespace}/workflows/{execution.workflow_id}\x12\xc9\x01\n\x11\x44\x65scribeTaskQueue\x12\x39.temporal.api.workflowservice.v1.DescribeTaskQueueRequest\x1a:.temporal.api.workflowservice.v1.DescribeTaskQueueResponse"=\x82\xd3\xe4\x93\x02\x37\x12\x35/namespaces/{namespace}/task-queues/{task_queue.name}\x12\x98\x01\n\x0eGetClusterInfo\x12\x36.temporal.api.workflowservice.v1.GetClusterInfoRequest\x1a\x37.temporal.api.workflowservice.v1.GetClusterInfoResponse"\x15\x82\xd3\xe4\x93\x02\x0f\x12\r/cluster-info\x12\x94\x01\n\rGetSystemInfo\x12\x35.temporal.api.workflowservice.v1.GetSystemInfoRequest\x1a\x36.temporal.api.workflowservice.v1.GetSystemInfoResponse"\x14\x82\xd3\xe4\x93\x02\x0e\x12\x0c/system-info\x12\x9e\x01\n\x17ListTaskQueuePartitions\x12?.temporal.api.workflowservice.v1.ListTaskQueuePartitionsRequest\x1a@.temporal.api.workflowservice.v1.ListTaskQueuePartitionsResponse"\x00\x12\xbd\x01\n\x0e\x43reateSchedule\x12\x36.temporal.api.workflowservice.v1.CreateScheduleRequest\x1a\x37.temporal.api.workflowservice.v1.CreateScheduleResponse":\x82\xd3\xe4\x93\x02\x34"//namespaces/{namespace}/schedules/{schedule_id}:\x01*\x12\xc0\x01\n\x10\x44\x65scribeSchedule\x12\x38.temporal.api.workflowservice.v1.DescribeScheduleRequest\x1a\x39.temporal.api.workflowservice.v1.DescribeScheduleResponse"7\x82\xd3\xe4\x93\x02\x31\x12//namespaces/{namespace}/schedules/{schedule_id}\x12\xc4\x01\n\x0eUpdateSchedule\x12\x36.temporal.api.workflowservice.v1.UpdateScheduleRequest\x1a\x37.temporal.api.workflowservice.v1.UpdateScheduleResponse"A\x82\xd3\xe4\x93\x02;"6/namespaces/{namespace}/schedules/{schedule_id}/update:\x01*\x12\xc0\x01\n\rPatchSchedule\x12\x35.temporal.api.workflowservice.v1.PatchScheduleRequest\x1a\x36.temporal.api.workflowservice.v1.PatchScheduleResponse"@\x82\xd3\xe4\x93\x02:"5/namespaces/{namespace}/schedules/{schedule_id}/patch:\x01*\x12\xea\x01\n\x19ListScheduleMatchingTimes\x12\x41.temporal.api.workflowservice.v1.ListScheduleMatchingTimesRequest\x1a\x42.temporal.api.workflowservice.v1.ListScheduleMatchingTimesResponse"F\x82\xd3\xe4\x93\x02@\x12>/namespaces/{namespace}/schedules/{schedule_id}/matching-times\x12\xba\x01\n\x0e\x44\x65leteSchedule\x12\x36.temporal.api.workflowservice.v1.DeleteScheduleRequest\x1a\x37.temporal.api.workflowservice.v1.DeleteScheduleResponse"7\x82\xd3\xe4\x93\x02\x31*//namespaces/{namespace}/schedules/{schedule_id}\x12\xa9\x01\n\rListSchedules\x12\x35.temporal.api.workflowservice.v1.ListSchedulesRequest\x1a\x36.temporal.api.workflowservice.v1.ListSchedulesResponse")\x82\xd3\xe4\x93\x02#\x12!/namespaces/{namespace}/schedules\x12\xb9\x01\n UpdateWorkerBuildIdCompatibility\x12H.temporal.api.workflowservice.v1.UpdateWorkerBuildIdCompatibilityRequest\x1aI.temporal.api.workflowservice.v1.UpdateWorkerBuildIdCompatibilityResponse"\x00\x12\x86\x02\n\x1dGetWorkerBuildIdCompatibility\x12\x45.temporal.api.workflowservice.v1.GetWorkerBuildIdCompatibilityRequest\x1a\x46.temporal.api.workflowservice.v1.GetWorkerBuildIdCompatibilityResponse"V\x82\xd3\xe4\x93\x02P\x12N/namespaces/{namespace}/task-queues/{task_queue}/worker-build-id-compatibility\x12\xaa\x01\n\x1bUpdateWorkerVersioningRules\x12\x43.temporal.api.workflowservice.v1.UpdateWorkerVersioningRulesRequest\x1a\x44.temporal.api.workflowservice.v1.UpdateWorkerVersioningRulesResponse"\x00\x12\xf1\x01\n\x18GetWorkerVersioningRules\x12@.temporal.api.workflowservice.v1.GetWorkerVersioningRulesRequest\x1a\x41.temporal.api.workflowservice.v1.GetWorkerVersioningRulesResponse"P\x82\xd3\xe4\x93\x02J\x12H/namespaces/{namespace}/task-queues/{task_queue}/worker-versioning-rules\x12\xdc\x01\n\x19GetWorkerTaskReachability\x12\x41.temporal.api.workflowservice.v1.GetWorkerTaskReachabilityRequest\x1a\x42.temporal.api.workflowservice.v1.GetWorkerTaskReachabilityResponse"8\x82\xd3\xe4\x93\x02\x32\x12\x30/namespaces/{namespace}/worker-task-reachability\x12\x87\x02\n\x17UpdateWorkflowExecution\x12?.temporal.api.workflowservice.v1.UpdateWorkflowExecutionRequest\x1a@.temporal.api.workflowservice.v1.UpdateWorkflowExecutionResponse"i\x82\xd3\xe4\x93\x02\x63"^/namespaces/{namespace}/workflows/{workflow_execution.workflow_id}/update/{request.input.name}:\x01*\x12\xaa\x01\n\x1bPollWorkflowExecutionUpdate\x12\x43.temporal.api.workflowservice.v1.PollWorkflowExecutionUpdateRequest\x1a\x44.temporal.api.workflowservice.v1.PollWorkflowExecutionUpdateResponse"\x00\x12\xce\x01\n\x13StartBatchOperation\x12;.temporal.api.workflowservice.v1.StartBatchOperationRequest\x1a<.temporal.api.workflowservice.v1.StartBatchOperationResponse"<\x82\xd3\xe4\x93\x02\x36"1/namespaces/{namespace}/batch-operations/{job_id}:\x01*\x12\xd0\x01\n\x12StopBatchOperation\x12:.temporal.api.workflowservice.v1.StopBatchOperationRequest\x1a;.temporal.api.workflowservice.v1.StopBatchOperationResponse"A\x82\xd3\xe4\x93\x02;"6/namespaces/{namespace}/batch-operations/{job_id}/stop:\x01*\x12\xd4\x01\n\x16\x44\x65scribeBatchOperation\x12>.temporal.api.workflowservice.v1.DescribeBatchOperationRequest\x1a?.temporal.api.workflowservice.v1.DescribeBatchOperationResponse"9\x82\xd3\xe4\x93\x02\x33\x12\x31/namespaces/{namespace}/batch-operations/{job_id}\x12\xc2\x01\n\x13ListBatchOperations\x12;.temporal.api.workflowservice.v1.ListBatchOperationsRequest\x1a<.temporal.api.workflowservice.v1.ListBatchOperationsResponse"0\x82\xd3\xe4\x93\x02*\x12(/namespaces/{namespace}/batch-operations\x12\x8f\x01\n\x12PollNexusTaskQueue\x12:.temporal.api.workflowservice.v1.PollNexusTaskQueueRequest\x1a;.temporal.api.workflowservice.v1.PollNexusTaskQueueResponse"\x00\x12\xa4\x01\n\x19RespondNexusTaskCompleted\x12\x41.temporal.api.workflowservice.v1.RespondNexusTaskCompletedRequest\x1a\x42.temporal.api.workflowservice.v1.RespondNexusTaskCompletedResponse"\x00\x12\x9b\x01\n\x16RespondNexusTaskFailed\x12>.temporal.api.workflowservice.v1.RespondNexusTaskFailedRequest\x1a?.temporal.api.workflowservice.v1.RespondNexusTaskFailedResponse"\x00\x42\xb6\x01\n"io.temporal.api.workflowservice.v1B\x0cServiceProtoP\x01Z5go.temporal.io/api/workflowservice/v1;workflowservice\xaa\x02!Temporalio.Api.WorkflowService.V1\xea\x02$Temporalio::Api::WorkflowService::V1b\x06proto3'
)


_WORKFLOWSERVICE = DESCRIPTOR.services_by_name["WorkflowService"]
if _descriptor._USE_C_DESCRIPTORS == False:
    DESCRIPTOR._options = None
    DESCRIPTOR._serialized_options = b'\n"io.temporal.api.workflowservice.v1B\014ServiceProtoP\001Z5go.temporal.io/api/workflowservice/v1;workflowservice\252\002!Temporalio.Api.WorkflowService.V1\352\002$Temporalio::Api::WorkflowService::V1'
    _WORKFLOWSERVICE.methods_by_name["RegisterNamespace"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "RegisterNamespace"
    ]._serialized_options = b'\202\323\344\223\002\020"\013/namespaces:\001*'
    _WORKFLOWSERVICE.methods_by_name["DescribeNamespace"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "DescribeNamespace"
    ]._serialized_options = b"\202\323\344\223\002\031\022\027/namespaces/{namespace}"
    _WORKFLOWSERVICE.methods_by_name["ListNamespaces"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "ListNamespaces"
    ]._serialized_options = b"\202\323\344\223\002\r\022\013/namespaces"
    _WORKFLOWSERVICE.methods_by_name["UpdateNamespace"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "UpdateNamespace"
    ]._serialized_options = (
        b'\202\323\344\223\002#"\036/namespaces/{namespace}/update:\001*'
    )
    _WORKFLOWSERVICE.methods_by_name["StartWorkflowExecution"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "StartWorkflowExecution"
    ]._serialized_options = (
        b'\202\323\344\223\0024"//namespaces/{namespace}/workflows/{workflow_id}:\001*'
    )
    _WORKFLOWSERVICE.methods_by_name["ExecuteMultiOperation"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "ExecuteMultiOperation"
    ]._serialized_options = b'\202\323\344\223\002>"9/namespaces/{namespace}/workflows/execute-multi-operation:\001*'
    _WORKFLOWSERVICE.methods_by_name["GetWorkflowExecutionHistory"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "GetWorkflowExecutionHistory"
    ]._serialized_options = b"\202\323\344\223\002C\022A/namespaces/{namespace}/workflows/{execution.workflow_id}/history"
    _WORKFLOWSERVICE.methods_by_name[
        "GetWorkflowExecutionHistoryReverse"
    ]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "GetWorkflowExecutionHistoryReverse"
    ]._serialized_options = b"\202\323\344\223\002K\022I/namespaces/{namespace}/workflows/{execution.workflow_id}/history-reverse"
    _WORKFLOWSERVICE.methods_by_name["RecordActivityTaskHeartbeat"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "RecordActivityTaskHeartbeat"
    ]._serialized_options = (
        b'\202\323\344\223\0021",/namespaces/{namespace}/activities/heartbeat:\001*'
    )
    _WORKFLOWSERVICE.methods_by_name["RecordActivityTaskHeartbeatById"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "RecordActivityTaskHeartbeatById"
    ]._serialized_options = b'\202\323\344\223\0027"2/namespaces/{namespace}/activities/heartbeat-by-id:\001*'
    _WORKFLOWSERVICE.methods_by_name["RespondActivityTaskCompleted"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "RespondActivityTaskCompleted"
    ]._serialized_options = (
        b'\202\323\344\223\0020"+/namespaces/{namespace}/activities/complete:\001*'
    )
    _WORKFLOWSERVICE.methods_by_name["RespondActivityTaskCompletedById"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "RespondActivityTaskCompletedById"
    ]._serialized_options = b'\202\323\344\223\0026"1/namespaces/{namespace}/activities/complete-by-id:\001*'
    _WORKFLOWSERVICE.methods_by_name["RespondActivityTaskFailed"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "RespondActivityTaskFailed"
    ]._serialized_options = (
        b"\202\323\344\223\002,\"'/namespaces/{namespace}/activities/fail:\001*"
    )
    _WORKFLOWSERVICE.methods_by_name["RespondActivityTaskFailedById"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "RespondActivityTaskFailedById"
    ]._serialized_options = (
        b'\202\323\344\223\0022"-/namespaces/{namespace}/activities/fail-by-id:\001*'
    )
    _WORKFLOWSERVICE.methods_by_name["RespondActivityTaskCanceled"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "RespondActivityTaskCanceled"
    ]._serialized_options = (
        b'\202\323\344\223\002.")/namespaces/{namespace}/activities/cancel:\001*'
    )
    _WORKFLOWSERVICE.methods_by_name["RespondActivityTaskCanceledById"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "RespondActivityTaskCanceledById"
    ]._serialized_options = (
        b'\202\323\344\223\0024"//namespaces/{namespace}/activities/cancel-by-id:\001*'
    )
    _WORKFLOWSERVICE.methods_by_name["RequestCancelWorkflowExecution"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "RequestCancelWorkflowExecution"
    ]._serialized_options = b'\202\323\344\223\002N"I/namespaces/{namespace}/workflows/{workflow_execution.workflow_id}/cancel:\001*'
    _WORKFLOWSERVICE.methods_by_name["SignalWorkflowExecution"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "SignalWorkflowExecution"
    ]._serialized_options = b'\202\323\344\223\002\\"W/namespaces/{namespace}/workflows/{workflow_execution.workflow_id}/signal/{signal_name}:\001*'
    _WORKFLOWSERVICE.methods_by_name["SignalWithStartWorkflowExecution"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "SignalWithStartWorkflowExecution"
    ]._serialized_options = b'\202\323\344\223\002T"O/namespaces/{namespace}/workflows/{workflow_id}/signal-with-start/{signal_name}:\001*'
    _WORKFLOWSERVICE.methods_by_name["ResetWorkflowExecution"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "ResetWorkflowExecution"
    ]._serialized_options = b'\202\323\344\223\002M"H/namespaces/{namespace}/workflows/{workflow_execution.workflow_id}/reset:\001*'
    _WORKFLOWSERVICE.methods_by_name["TerminateWorkflowExecution"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "TerminateWorkflowExecution"
    ]._serialized_options = b'\202\323\344\223\002Q"L/namespaces/{namespace}/workflows/{workflow_execution.workflow_id}/terminate:\001*'
    _WORKFLOWSERVICE.methods_by_name["ListWorkflowExecutions"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "ListWorkflowExecutions"
    ]._serialized_options = (
        b"\202\323\344\223\002#\022!/namespaces/{namespace}/workflows"
    )
    _WORKFLOWSERVICE.methods_by_name["ListArchivedWorkflowExecutions"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "ListArchivedWorkflowExecutions"
    ]._serialized_options = (
        b"\202\323\344\223\002,\022*/namespaces/{namespace}/archived-workflows"
    )
    _WORKFLOWSERVICE.methods_by_name["CountWorkflowExecutions"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "CountWorkflowExecutions"
    ]._serialized_options = (
        b"\202\323\344\223\002(\022&/namespaces/{namespace}/workflow-count"
    )
    _WORKFLOWSERVICE.methods_by_name["QueryWorkflow"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "QueryWorkflow"
    ]._serialized_options = b'\202\323\344\223\002W"R/namespaces/{namespace}/workflows/{execution.workflow_id}/query/{query.query_type}:\001*'
    _WORKFLOWSERVICE.methods_by_name["DescribeWorkflowExecution"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "DescribeWorkflowExecution"
    ]._serialized_options = b"\202\323\344\223\002;\0229/namespaces/{namespace}/workflows/{execution.workflow_id}"
    _WORKFLOWSERVICE.methods_by_name["DescribeTaskQueue"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "DescribeTaskQueue"
    ]._serialized_options = b"\202\323\344\223\0027\0225/namespaces/{namespace}/task-queues/{task_queue.name}"
    _WORKFLOWSERVICE.methods_by_name["GetClusterInfo"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "GetClusterInfo"
    ]._serialized_options = b"\202\323\344\223\002\017\022\r/cluster-info"
    _WORKFLOWSERVICE.methods_by_name["GetSystemInfo"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "GetSystemInfo"
    ]._serialized_options = b"\202\323\344\223\002\016\022\014/system-info"
    _WORKFLOWSERVICE.methods_by_name["CreateSchedule"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "CreateSchedule"
    ]._serialized_options = (
        b'\202\323\344\223\0024"//namespaces/{namespace}/schedules/{schedule_id}:\001*'
    )
    _WORKFLOWSERVICE.methods_by_name["DescribeSchedule"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "DescribeSchedule"
    ]._serialized_options = (
        b"\202\323\344\223\0021\022//namespaces/{namespace}/schedules/{schedule_id}"
    )
    _WORKFLOWSERVICE.methods_by_name["UpdateSchedule"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "UpdateSchedule"
    ]._serialized_options = b'\202\323\344\223\002;"6/namespaces/{namespace}/schedules/{schedule_id}/update:\001*'
    _WORKFLOWSERVICE.methods_by_name["PatchSchedule"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "PatchSchedule"
    ]._serialized_options = b'\202\323\344\223\002:"5/namespaces/{namespace}/schedules/{schedule_id}/patch:\001*'
    _WORKFLOWSERVICE.methods_by_name["ListScheduleMatchingTimes"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "ListScheduleMatchingTimes"
    ]._serialized_options = b"\202\323\344\223\002@\022>/namespaces/{namespace}/schedules/{schedule_id}/matching-times"
    _WORKFLOWSERVICE.methods_by_name["DeleteSchedule"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "DeleteSchedule"
    ]._serialized_options = (
        b"\202\323\344\223\0021*//namespaces/{namespace}/schedules/{schedule_id}"
    )
    _WORKFLOWSERVICE.methods_by_name["ListSchedules"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "ListSchedules"
    ]._serialized_options = (
        b"\202\323\344\223\002#\022!/namespaces/{namespace}/schedules"
    )
    _WORKFLOWSERVICE.methods_by_name["GetWorkerBuildIdCompatibility"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "GetWorkerBuildIdCompatibility"
    ]._serialized_options = b"\202\323\344\223\002P\022N/namespaces/{namespace}/task-queues/{task_queue}/worker-build-id-compatibility"
    _WORKFLOWSERVICE.methods_by_name["GetWorkerVersioningRules"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "GetWorkerVersioningRules"
    ]._serialized_options = b"\202\323\344\223\002J\022H/namespaces/{namespace}/task-queues/{task_queue}/worker-versioning-rules"
    _WORKFLOWSERVICE.methods_by_name["GetWorkerTaskReachability"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "GetWorkerTaskReachability"
    ]._serialized_options = (
        b"\202\323\344\223\0022\0220/namespaces/{namespace}/worker-task-reachability"
    )
    _WORKFLOWSERVICE.methods_by_name["UpdateWorkflowExecution"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "UpdateWorkflowExecution"
    ]._serialized_options = b'\202\323\344\223\002c"^/namespaces/{namespace}/workflows/{workflow_execution.workflow_id}/update/{request.input.name}:\001*'
    _WORKFLOWSERVICE.methods_by_name["StartBatchOperation"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "StartBatchOperation"
    ]._serialized_options = b'\202\323\344\223\0026"1/namespaces/{namespace}/batch-operations/{job_id}:\001*'
    _WORKFLOWSERVICE.methods_by_name["StopBatchOperation"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "StopBatchOperation"
    ]._serialized_options = b'\202\323\344\223\002;"6/namespaces/{namespace}/batch-operations/{job_id}/stop:\001*'
    _WORKFLOWSERVICE.methods_by_name["DescribeBatchOperation"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "DescribeBatchOperation"
    ]._serialized_options = (
        b"\202\323\344\223\0023\0221/namespaces/{namespace}/batch-operations/{job_id}"
    )
    _WORKFLOWSERVICE.methods_by_name["ListBatchOperations"]._options = None
    _WORKFLOWSERVICE.methods_by_name[
        "ListBatchOperations"
    ]._serialized_options = (
        b"\202\323\344\223\002*\022(/namespaces/{namespace}/batch-operations"
    )
    _WORKFLOWSERVICE._serialized_start = 169
    _WORKFLOWSERVICE._serialized_end = 12900
# @@protoc_insertion_point(module_scope)
