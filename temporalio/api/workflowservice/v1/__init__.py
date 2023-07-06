from .request_response_pb2 import (
    CountWorkflowExecutionsRequest,
    CountWorkflowExecutionsResponse,
    CreateScheduleRequest,
    CreateScheduleResponse,
    DeleteScheduleRequest,
    DeleteScheduleResponse,
    DeleteWorkflowExecutionRequest,
    DeleteWorkflowExecutionResponse,
    DeprecateNamespaceRequest,
    DeprecateNamespaceResponse,
    DescribeBatchOperationRequest,
    DescribeBatchOperationResponse,
    DescribeNamespaceRequest,
    DescribeNamespaceResponse,
    DescribeScheduleRequest,
    DescribeScheduleResponse,
    DescribeTaskQueueRequest,
    DescribeTaskQueueResponse,
    DescribeWorkflowExecutionRequest,
    DescribeWorkflowExecutionResponse,
    GetClusterInfoRequest,
    GetClusterInfoResponse,
    GetSearchAttributesRequest,
    GetSearchAttributesResponse,
    GetSystemInfoRequest,
    GetSystemInfoResponse,
    GetWorkerBuildIdCompatibilityRequest,
    GetWorkerBuildIdCompatibilityResponse,
    GetWorkerTaskReachabilityRequest,
    GetWorkerTaskReachabilityResponse,
    GetWorkflowExecutionHistoryRequest,
    GetWorkflowExecutionHistoryResponse,
    GetWorkflowExecutionHistoryReverseRequest,
    GetWorkflowExecutionHistoryReverseResponse,
    ListArchivedWorkflowExecutionsRequest,
    ListArchivedWorkflowExecutionsResponse,
    ListBatchOperationsRequest,
    ListBatchOperationsResponse,
    ListClosedWorkflowExecutionsRequest,
    ListClosedWorkflowExecutionsResponse,
    ListNamespacesRequest,
    ListNamespacesResponse,
    ListOpenWorkflowExecutionsRequest,
    ListOpenWorkflowExecutionsResponse,
    ListScheduleMatchingTimesRequest,
    ListScheduleMatchingTimesResponse,
    ListSchedulesRequest,
    ListSchedulesResponse,
    ListTaskQueuePartitionsRequest,
    ListTaskQueuePartitionsResponse,
    ListWorkflowExecutionsRequest,
    ListWorkflowExecutionsResponse,
    PatchScheduleRequest,
    PatchScheduleResponse,
    PollActivityTaskQueueRequest,
    PollActivityTaskQueueResponse,
    PollWorkflowExecutionUpdateRequest,
    PollWorkflowExecutionUpdateResponse,
    PollWorkflowTaskQueueRequest,
    PollWorkflowTaskQueueResponse,
    QueryWorkflowRequest,
    QueryWorkflowResponse,
    RecordActivityTaskHeartbeatByIdRequest,
    RecordActivityTaskHeartbeatByIdResponse,
    RecordActivityTaskHeartbeatRequest,
    RecordActivityTaskHeartbeatResponse,
    RegisterNamespaceRequest,
    RegisterNamespaceResponse,
    RequestCancelWorkflowExecutionRequest,
    RequestCancelWorkflowExecutionResponse,
    ResetStickyTaskQueueRequest,
    ResetStickyTaskQueueResponse,
    ResetWorkflowExecutionRequest,
    ResetWorkflowExecutionResponse,
    RespondActivityTaskCanceledByIdRequest,
    RespondActivityTaskCanceledByIdResponse,
    RespondActivityTaskCanceledRequest,
    RespondActivityTaskCanceledResponse,
    RespondActivityTaskCompletedByIdRequest,
    RespondActivityTaskCompletedByIdResponse,
    RespondActivityTaskCompletedRequest,
    RespondActivityTaskCompletedResponse,
    RespondActivityTaskFailedByIdRequest,
    RespondActivityTaskFailedByIdResponse,
    RespondActivityTaskFailedRequest,
    RespondActivityTaskFailedResponse,
    RespondQueryTaskCompletedRequest,
    RespondQueryTaskCompletedResponse,
    RespondWorkflowTaskCompletedRequest,
    RespondWorkflowTaskCompletedResponse,
    RespondWorkflowTaskFailedRequest,
    RespondWorkflowTaskFailedResponse,
    ScanWorkflowExecutionsRequest,
    ScanWorkflowExecutionsResponse,
    SignalWithStartWorkflowExecutionRequest,
    SignalWithStartWorkflowExecutionResponse,
    SignalWorkflowExecutionRequest,
    SignalWorkflowExecutionResponse,
    StartBatchOperationRequest,
    StartBatchOperationResponse,
    StartWorkflowExecutionRequest,
    StartWorkflowExecutionResponse,
    StopBatchOperationRequest,
    StopBatchOperationResponse,
    TerminateWorkflowExecutionRequest,
    TerminateWorkflowExecutionResponse,
    UpdateNamespaceRequest,
    UpdateNamespaceResponse,
    UpdateScheduleRequest,
    UpdateScheduleResponse,
    UpdateWorkerBuildIdCompatibilityRequest,
    UpdateWorkerBuildIdCompatibilityResponse,
    UpdateWorkflowExecutionRequest,
    UpdateWorkflowExecutionResponse,
)

__all__ = [
    "CountWorkflowExecutionsRequest",
    "CountWorkflowExecutionsResponse",
    "CreateScheduleRequest",
    "CreateScheduleResponse",
    "DeleteScheduleRequest",
    "DeleteScheduleResponse",
    "DeleteWorkflowExecutionRequest",
    "DeleteWorkflowExecutionResponse",
    "DeprecateNamespaceRequest",
    "DeprecateNamespaceResponse",
    "DescribeBatchOperationRequest",
    "DescribeBatchOperationResponse",
    "DescribeNamespaceRequest",
    "DescribeNamespaceResponse",
    "DescribeScheduleRequest",
    "DescribeScheduleResponse",
    "DescribeTaskQueueRequest",
    "DescribeTaskQueueResponse",
    "DescribeWorkflowExecutionRequest",
    "DescribeWorkflowExecutionResponse",
    "GetClusterInfoRequest",
    "GetClusterInfoResponse",
    "GetSearchAttributesRequest",
    "GetSearchAttributesResponse",
    "GetSystemInfoRequest",
    "GetSystemInfoResponse",
    "GetWorkerBuildIdCompatibilityRequest",
    "GetWorkerBuildIdCompatibilityResponse",
    "GetWorkerTaskReachabilityRequest",
    "GetWorkerTaskReachabilityResponse",
    "GetWorkflowExecutionHistoryRequest",
    "GetWorkflowExecutionHistoryResponse",
    "GetWorkflowExecutionHistoryReverseRequest",
    "GetWorkflowExecutionHistoryReverseResponse",
    "ListArchivedWorkflowExecutionsRequest",
    "ListArchivedWorkflowExecutionsResponse",
    "ListBatchOperationsRequest",
    "ListBatchOperationsResponse",
    "ListClosedWorkflowExecutionsRequest",
    "ListClosedWorkflowExecutionsResponse",
    "ListNamespacesRequest",
    "ListNamespacesResponse",
    "ListOpenWorkflowExecutionsRequest",
    "ListOpenWorkflowExecutionsResponse",
    "ListScheduleMatchingTimesRequest",
    "ListScheduleMatchingTimesResponse",
    "ListSchedulesRequest",
    "ListSchedulesResponse",
    "ListTaskQueuePartitionsRequest",
    "ListTaskQueuePartitionsResponse",
    "ListWorkflowExecutionsRequest",
    "ListWorkflowExecutionsResponse",
    "PatchScheduleRequest",
    "PatchScheduleResponse",
    "PollActivityTaskQueueRequest",
    "PollActivityTaskQueueResponse",
    "PollWorkflowExecutionUpdateRequest",
    "PollWorkflowExecutionUpdateResponse",
    "PollWorkflowTaskQueueRequest",
    "PollWorkflowTaskQueueResponse",
    "QueryWorkflowRequest",
    "QueryWorkflowResponse",
    "RecordActivityTaskHeartbeatByIdRequest",
    "RecordActivityTaskHeartbeatByIdResponse",
    "RecordActivityTaskHeartbeatRequest",
    "RecordActivityTaskHeartbeatResponse",
    "RegisterNamespaceRequest",
    "RegisterNamespaceResponse",
    "RequestCancelWorkflowExecutionRequest",
    "RequestCancelWorkflowExecutionResponse",
    "ResetStickyTaskQueueRequest",
    "ResetStickyTaskQueueResponse",
    "ResetWorkflowExecutionRequest",
    "ResetWorkflowExecutionResponse",
    "RespondActivityTaskCanceledByIdRequest",
    "RespondActivityTaskCanceledByIdResponse",
    "RespondActivityTaskCanceledRequest",
    "RespondActivityTaskCanceledResponse",
    "RespondActivityTaskCompletedByIdRequest",
    "RespondActivityTaskCompletedByIdResponse",
    "RespondActivityTaskCompletedRequest",
    "RespondActivityTaskCompletedResponse",
    "RespondActivityTaskFailedByIdRequest",
    "RespondActivityTaskFailedByIdResponse",
    "RespondActivityTaskFailedRequest",
    "RespondActivityTaskFailedResponse",
    "RespondQueryTaskCompletedRequest",
    "RespondQueryTaskCompletedResponse",
    "RespondWorkflowTaskCompletedRequest",
    "RespondWorkflowTaskCompletedResponse",
    "RespondWorkflowTaskFailedRequest",
    "RespondWorkflowTaskFailedResponse",
    "ScanWorkflowExecutionsRequest",
    "ScanWorkflowExecutionsResponse",
    "SignalWithStartWorkflowExecutionRequest",
    "SignalWithStartWorkflowExecutionResponse",
    "SignalWorkflowExecutionRequest",
    "SignalWorkflowExecutionResponse",
    "StartBatchOperationRequest",
    "StartBatchOperationResponse",
    "StartWorkflowExecutionRequest",
    "StartWorkflowExecutionResponse",
    "StopBatchOperationRequest",
    "StopBatchOperationResponse",
    "TerminateWorkflowExecutionRequest",
    "TerminateWorkflowExecutionResponse",
    "UpdateNamespaceRequest",
    "UpdateNamespaceResponse",
    "UpdateScheduleRequest",
    "UpdateScheduleResponse",
    "UpdateWorkerBuildIdCompatibilityRequest",
    "UpdateWorkerBuildIdCompatibilityResponse",
    "UpdateWorkflowExecutionRequest",
    "UpdateWorkflowExecutionResponse",
]

# gRPC is optional
try:
    import grpc

    from .service_pb2_grpc import (
        WorkflowServiceServicer,
        WorkflowServiceStub,
        add_WorkflowServiceServicer_to_server,
    )

    __all__.extend(
        [
            "WorkflowServiceServicer",
            "WorkflowServiceStub",
            "add_WorkflowServiceServicer_to_server",
        ]
    )
except ImportError:
    pass
