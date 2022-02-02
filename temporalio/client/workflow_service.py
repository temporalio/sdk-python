from abc import ABC, abstractmethod
from typing import Generic, Type, TypeVar

import google.protobuf.message

import temporalio.api.workflowservice.v1
import temporalio.bridge.client

WorkflowServiceRequest = TypeVar(
    "WorkflowServiceRequest", bound=google.protobuf.message.Message
)
WorkflowServiceResponse = TypeVar(
    "WorkflowServiceResponse", bound=google.protobuf.message.Message
)


class WorkflowService(ABC):
    @staticmethod
    async def connect(target_url: str) -> "WorkflowService":
        return await BridgeWorkflowService.connect(target_url=target_url)

    def __init__(self) -> None:
        super().__init__()

        wsv1 = temporalio.api.workflowservice.v1

        self.count_workflow_executions = self.__new_call(
            "count_workflow_executions",
            wsv1.CountWorkflowExecutionsRequest,
            wsv1.CountWorkflowExecutionsResponse,
        )
        self.deprecate_namespace = self.__new_call(
            "deprecate_namespace",
            wsv1.DeprecateNamespaceRequest,
            wsv1.DeprecateNamespaceResponse,
        )
        self.describe_namespace = self.__new_call(
            "describe_namespace",
            wsv1.DescribeNamespaceRequest,
            wsv1.DescribeNamespaceResponse,
        )
        self.describe_task_queue = self.__new_call(
            "describe_task_queue",
            wsv1.DescribeTaskQueueRequest,
            wsv1.DescribeTaskQueueResponse,
        )
        self.describe_workflow_execution = self.__new_call(
            "describe_workflow_execution",
            wsv1.DescribeWorkflowExecutionRequest,
            wsv1.DescribeWorkflowExecutionResponse,
        )
        self.get_cluster_info = self.__new_call(
            "get_cluster_info",
            wsv1.GetClusterInfoRequest,
            wsv1.GetClusterInfoResponse,
        )
        self.get_search_attributes = self.__new_call(
            "get_search_attributes",
            wsv1.GetSearchAttributesRequest,
            wsv1.GetSearchAttributesResponse,
        )
        self.get_workflow_execution_history = self.__new_call(
            "get_workflow_execution_history",
            wsv1.GetWorkflowExecutionHistoryRequest,
            wsv1.GetWorkflowExecutionHistoryResponse,
        )
        self.list_archived_workflow_executions = self.__new_call(
            "list_archived_workflow_executions",
            wsv1.ListArchivedWorkflowExecutionsRequest,
            wsv1.ListArchivedWorkflowExecutionsResponse,
        )
        self.list_closed_workflow_executions = self.__new_call(
            "list_closed_workflow_executions",
            wsv1.ListClosedWorkflowExecutionsRequest,
            wsv1.ListClosedWorkflowExecutionsResponse,
        )
        self.list_namespaces = self.__new_call(
            "list_namespaces",
            wsv1.ListNamespacesRequest,
            wsv1.ListNamespacesResponse,
        )
        self.list_open_workflow_executions = self.__new_call(
            "list_open_workflow_executions",
            wsv1.ListOpenWorkflowExecutionsRequest,
            wsv1.ListOpenWorkflowExecutionsResponse,
        )
        self.list_task_queue_partitions = self.__new_call(
            "list_task_queue_partitions",
            wsv1.ListTaskQueuePartitionsRequest,
            wsv1.ListTaskQueuePartitionsResponse,
        )
        self.list_workflow_executions = self.__new_call(
            "list_workflow_executions",
            wsv1.ListWorkflowExecutionsRequest,
            wsv1.ListWorkflowExecutionsResponse,
        )
        self.poll_activity_task_queue = self.__new_call(
            "poll_activity_task_queue",
            wsv1.PollActivityTaskQueueRequest,
            wsv1.PollActivityTaskQueueResponse,
        )
        self.poll_workflow_task_queue = self.__new_call(
            "poll_workflow_task_queue",
            wsv1.PollWorkflowTaskQueueRequest,
            wsv1.PollWorkflowTaskQueueResponse,
        )
        self.query_workflow = self.__new_call(
            "query_workflow",
            wsv1.QueryWorkflowRequest,
            wsv1.QueryWorkflowResponse,
        )
        self.record_activity_task_heartbeat = self.__new_call(
            "record_activity_task_heartbeat",
            wsv1.RecordActivityTaskHeartbeatRequest,
            wsv1.RecordActivityTaskHeartbeatResponse,
        )
        self.record_activity_task_heartbeat_by_id = self.__new_call(
            "record_activity_task_heartbeat_by_id",
            wsv1.RecordActivityTaskHeartbeatByIdRequest,
            wsv1.RecordActivityTaskHeartbeatByIdResponse,
        )
        self.register_namespace = self.__new_call(
            "register_namespace",
            wsv1.RegisterNamespaceRequest,
            wsv1.RegisterNamespaceResponse,
        )
        self.request_cancel_workflow_execution = self.__new_call(
            "request_cancel_workflow_execution",
            wsv1.RequestCancelWorkflowExecutionRequest,
            wsv1.RequestCancelWorkflowExecutionResponse,
        )
        self.reset_sticky_task_queue = self.__new_call(
            "reset_sticky_task_queue",
            wsv1.ResetStickyTaskQueueRequest,
            wsv1.ResetStickyTaskQueueResponse,
        )
        self.reset_workflow_execution = self.__new_call(
            "reset_workflow_execution",
            wsv1.ResetWorkflowExecutionRequest,
            wsv1.ResetWorkflowExecutionResponse,
        )
        self.respond_activity_task_canceled = self.__new_call(
            "respond_activity_task_canceled",
            wsv1.RespondActivityTaskCanceledRequest,
            wsv1.RespondActivityTaskCanceledResponse,
        )
        self.respond_activity_task_canceled_by_id = self.__new_call(
            "respond_activity_task_canceled_by_id",
            wsv1.RespondActivityTaskCanceledByIdRequest,
            wsv1.RespondActivityTaskCanceledByIdResponse,
        )
        self.respond_activity_task_completed = self.__new_call(
            "respond_activity_task_completed",
            wsv1.RespondActivityTaskCompletedRequest,
            wsv1.RespondActivityTaskCompletedResponse,
        )
        self.respond_activity_task_completed_by_id = self.__new_call(
            "respond_activity_task_completed_by_id",
            wsv1.RespondActivityTaskCompletedByIdRequest,
            wsv1.RespondActivityTaskCompletedByIdResponse,
        )
        self.respond_activity_task_failed = self.__new_call(
            "respond_activity_task_failed",
            wsv1.RespondActivityTaskFailedRequest,
            wsv1.RespondActivityTaskFailedResponse,
        )
        self.respond_activity_task_failed_by_id = self.__new_call(
            "respond_activity_task_failed_by_id",
            wsv1.RespondActivityTaskFailedByIdRequest,
            wsv1.RespondActivityTaskFailedByIdResponse,
        )
        self.respond_query_task_completed = self.__new_call(
            "respond_query_task_completed",
            wsv1.RespondQueryTaskCompletedRequest,
            wsv1.RespondQueryTaskCompletedResponse,
        )
        self.respond_workflow_task_completed = self.__new_call(
            "respond_workflow_task_completed",
            wsv1.RespondWorkflowTaskCompletedRequest,
            wsv1.RespondWorkflowTaskCompletedResponse,
        )
        self.respond_workflow_task_failed = self.__new_call(
            "respond_workflow_task_failed",
            wsv1.RespondWorkflowTaskFailedRequest,
            wsv1.RespondWorkflowTaskFailedResponse,
        )
        self.scan_workflow_executions = self.__new_call(
            "scan_workflow_executions",
            wsv1.ScanWorkflowExecutionsRequest,
            wsv1.ScanWorkflowExecutionsResponse,
        )
        self.signal_with_start_workflow_execution = self.__new_call(
            "signal_with_start_workflow_execution",
            wsv1.SignalWithStartWorkflowExecutionRequest,
            wsv1.SignalWithStartWorkflowExecutionResponse,
        )
        self.signal_workflow_execution = self.__new_call(
            "signal_workflow_execution",
            wsv1.SignalWorkflowExecutionRequest,
            wsv1.SignalWorkflowExecutionResponse,
        )
        self.terminate_workflow_execution = self.__new_call(
            "terminate_workflow_execution",
            wsv1.TerminateWorkflowExecutionRequest,
            wsv1.TerminateWorkflowExecutionResponse,
        )
        self.reset_workflow_execution = self.__new_call(
            "reset_workflow_execution",
            wsv1.ResetWorkflowExecutionRequest,
            wsv1.ResetWorkflowExecutionResponse,
        )
        self.update_namespace = self.__new_call(
            "update_namespace",
            wsv1.UpdateNamespaceRequest,
            wsv1.UpdateNamespaceResponse,
        )

    @abstractmethod
    async def _rpc_call(
        self,
        rpc: str,
        req: google.protobuf.message.Message,
        resp_type: Type[WorkflowServiceResponse],
        *,
        retry: bool = False,
    ) -> WorkflowServiceResponse:
        raise NotImplementedError

    def __new_call(
        self,
        name: str,
        req_type: Type[WorkflowServiceRequest],
        resp_type: Type[WorkflowServiceResponse],
    ) -> "WorkflowServiceCall[WorkflowServiceRequest, WorkflowServiceResponse]":
        return WorkflowServiceCall(self, name, req_type, resp_type)


class WorkflowServiceCall(Generic[WorkflowServiceRequest, WorkflowServiceResponse]):
    def __init__(
        self,
        service: WorkflowService,
        name: str,
        req_type: Type[WorkflowServiceRequest],
        resp_type: Type[WorkflowServiceResponse],
    ) -> None:
        self.service = service
        self.name = name
        self.resp_type = resp_type

    async def __call__(
        self, req: WorkflowServiceRequest, *, retry: bool = False
    ) -> WorkflowServiceResponse:
        return await self.service._rpc_call(self.name, req, self.resp_type, retry=retry)


class BridgeWorkflowService(WorkflowService):
    @staticmethod
    async def connect(target_url: str) -> "BridgeWorkflowService":
        return BridgeWorkflowService(
            await temporalio.bridge.client.Client.connect(
                temporalio.bridge.client.ClientOptions(target_url=target_url)
            )
        )

    _bridge_client: temporalio.bridge.client.Client

    def __init__(self, bridge_client: temporalio.bridge.client.Client) -> None:
        super().__init__()
        self._bridge_client = bridge_client

    async def _rpc_call(
        self,
        rpc: str,
        req: google.protobuf.message.Message,
        resp_type: Type[WorkflowServiceResponse],
        *,
        retry: bool = False,
    ) -> WorkflowServiceResponse:
        return await self._bridge_client.rpc_call(rpc, req, resp_type, retry=retry)
