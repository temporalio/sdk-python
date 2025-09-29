# Generated file. DO NOT EDIT

from __future__ import annotations

from datetime import timedelta
from typing import Mapping, Optional, Union, TYPE_CHECKING
import google.protobuf.empty_pb2

import temporalio.api.workflowservice.v1
import temporalio.api.operatorservice.v1
import temporalio.api.cloud.cloudservice.v1
import temporalio.api.testservice.v1
import temporalio.bridge.proto.health.v1


if TYPE_CHECKING:
    from temporalio.service import ServiceClient


class WorkflowService:
    def __init__(self, client: ServiceClient):
        self.client = client
        self.service = "workflow"

    async def count_workflow_executions(
        self,
        req: temporalio.api.workflowservice.v1.CountWorkflowExecutionsRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.CountWorkflowExecutionsResponse:
        print("sup from count_workflow_executions")
        return await self.client._rpc_call(
            rpc="count_workflow_executions",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.CountWorkflowExecutionsResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def create_schedule(
        self,
        req: temporalio.api.workflowservice.v1.CreateScheduleRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.CreateScheduleResponse:
        print("sup from create_schedule")
        return await self.client._rpc_call(
            rpc="create_schedule",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.CreateScheduleResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def create_workflow_rule(
        self,
        req: temporalio.api.workflowservice.v1.CreateWorkflowRuleRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.CreateWorkflowRuleResponse:
        print("sup from create_workflow_rule")
        return await self.client._rpc_call(
            rpc="create_workflow_rule",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.CreateWorkflowRuleResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def delete_schedule(
        self,
        req: temporalio.api.workflowservice.v1.DeleteScheduleRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.DeleteScheduleResponse:
        print("sup from delete_schedule")
        return await self.client._rpc_call(
            rpc="delete_schedule",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.DeleteScheduleResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def delete_worker_deployment(
        self,
        req: temporalio.api.workflowservice.v1.DeleteWorkerDeploymentRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.DeleteWorkerDeploymentResponse:
        print("sup from delete_worker_deployment")
        return await self.client._rpc_call(
            rpc="delete_worker_deployment",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.DeleteWorkerDeploymentResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def delete_worker_deployment_version(
        self,
        req: temporalio.api.workflowservice.v1.DeleteWorkerDeploymentVersionRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.DeleteWorkerDeploymentVersionResponse:
        print("sup from delete_worker_deployment_version")
        return await self.client._rpc_call(
            rpc="delete_worker_deployment_version",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.DeleteWorkerDeploymentVersionResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def delete_workflow_execution(
        self,
        req: temporalio.api.workflowservice.v1.DeleteWorkflowExecutionRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.DeleteWorkflowExecutionResponse:
        print("sup from delete_workflow_execution")
        return await self.client._rpc_call(
            rpc="delete_workflow_execution",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.DeleteWorkflowExecutionResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def delete_workflow_rule(
        self,
        req: temporalio.api.workflowservice.v1.DeleteWorkflowRuleRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.DeleteWorkflowRuleResponse:
        print("sup from delete_workflow_rule")
        return await self.client._rpc_call(
            rpc="delete_workflow_rule",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.DeleteWorkflowRuleResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def deprecate_namespace(
        self,
        req: temporalio.api.workflowservice.v1.DeprecateNamespaceRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.DeprecateNamespaceResponse:
        print("sup from deprecate_namespace")
        return await self.client._rpc_call(
            rpc="deprecate_namespace",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.DeprecateNamespaceResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def describe_batch_operation(
        self,
        req: temporalio.api.workflowservice.v1.DescribeBatchOperationRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.DescribeBatchOperationResponse:
        print("sup from describe_batch_operation")
        return await self.client._rpc_call(
            rpc="describe_batch_operation",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.DescribeBatchOperationResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def describe_deployment(
        self,
        req: temporalio.api.workflowservice.v1.DescribeDeploymentRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.DescribeDeploymentResponse:
        print("sup from describe_deployment")
        return await self.client._rpc_call(
            rpc="describe_deployment",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.DescribeDeploymentResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def describe_namespace(
        self,
        req: temporalio.api.workflowservice.v1.DescribeNamespaceRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.DescribeNamespaceResponse:
        print("sup from describe_namespace")
        return await self.client._rpc_call(
            rpc="describe_namespace",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.DescribeNamespaceResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def describe_schedule(
        self,
        req: temporalio.api.workflowservice.v1.DescribeScheduleRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.DescribeScheduleResponse:
        print("sup from describe_schedule")
        return await self.client._rpc_call(
            rpc="describe_schedule",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.DescribeScheduleResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def describe_task_queue(
        self,
        req: temporalio.api.workflowservice.v1.DescribeTaskQueueRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.DescribeTaskQueueResponse:
        print("sup from describe_task_queue")
        return await self.client._rpc_call(
            rpc="describe_task_queue",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.DescribeTaskQueueResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def describe_worker_deployment(
        self,
        req: temporalio.api.workflowservice.v1.DescribeWorkerDeploymentRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.DescribeWorkerDeploymentResponse:
        print("sup from describe_worker_deployment")
        return await self.client._rpc_call(
            rpc="describe_worker_deployment",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.DescribeWorkerDeploymentResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def describe_worker_deployment_version(
        self,
        req: temporalio.api.workflowservice.v1.DescribeWorkerDeploymentVersionRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.DescribeWorkerDeploymentVersionResponse:
        print("sup from describe_worker_deployment_version")
        return await self.client._rpc_call(
            rpc="describe_worker_deployment_version",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.DescribeWorkerDeploymentVersionResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def describe_workflow_execution(
        self,
        req: temporalio.api.workflowservice.v1.DescribeWorkflowExecutionRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.DescribeWorkflowExecutionResponse:
        print("sup from describe_workflow_execution")
        return await self.client._rpc_call(
            rpc="describe_workflow_execution",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.DescribeWorkflowExecutionResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def describe_workflow_rule(
        self,
        req: temporalio.api.workflowservice.v1.DescribeWorkflowRuleRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.DescribeWorkflowRuleResponse:
        print("sup from describe_workflow_rule")
        return await self.client._rpc_call(
            rpc="describe_workflow_rule",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.DescribeWorkflowRuleResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def execute_multi_operation(
        self,
        req: temporalio.api.workflowservice.v1.ExecuteMultiOperationRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.ExecuteMultiOperationResponse:
        print("sup from execute_multi_operation")
        return await self.client._rpc_call(
            rpc="execute_multi_operation",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.ExecuteMultiOperationResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def fetch_worker_config(
        self,
        req: temporalio.api.workflowservice.v1.FetchWorkerConfigRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.FetchWorkerConfigResponse:
        print("sup from fetch_worker_config")
        return await self.client._rpc_call(
            rpc="fetch_worker_config",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.FetchWorkerConfigResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_cluster_info(
        self,
        req: temporalio.api.workflowservice.v1.GetClusterInfoRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.GetClusterInfoResponse:
        print("sup from get_cluster_info")
        return await self.client._rpc_call(
            rpc="get_cluster_info",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.GetClusterInfoResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_current_deployment(
        self,
        req: temporalio.api.workflowservice.v1.GetCurrentDeploymentRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.GetCurrentDeploymentResponse:
        print("sup from get_current_deployment")
        return await self.client._rpc_call(
            rpc="get_current_deployment",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.GetCurrentDeploymentResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_deployment_reachability(
        self,
        req: temporalio.api.workflowservice.v1.GetDeploymentReachabilityRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.GetDeploymentReachabilityResponse:
        print("sup from get_deployment_reachability")
        return await self.client._rpc_call(
            rpc="get_deployment_reachability",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.GetDeploymentReachabilityResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_search_attributes(
        self,
        req: temporalio.api.workflowservice.v1.GetSearchAttributesRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.GetSearchAttributesResponse:
        print("sup from get_search_attributes")
        return await self.client._rpc_call(
            rpc="get_search_attributes",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.GetSearchAttributesResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_system_info(
        self,
        req: temporalio.api.workflowservice.v1.GetSystemInfoRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.GetSystemInfoResponse:
        print("sup from get_system_info")
        return await self.client._rpc_call(
            rpc="get_system_info",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.GetSystemInfoResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_worker_build_id_compatibility(
        self,
        req: temporalio.api.workflowservice.v1.GetWorkerBuildIdCompatibilityRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.GetWorkerBuildIdCompatibilityResponse:
        print("sup from get_worker_build_id_compatibility")
        return await self.client._rpc_call(
            rpc="get_worker_build_id_compatibility",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.GetWorkerBuildIdCompatibilityResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_worker_task_reachability(
        self,
        req: temporalio.api.workflowservice.v1.GetWorkerTaskReachabilityRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.GetWorkerTaskReachabilityResponse:
        print("sup from get_worker_task_reachability")
        return await self.client._rpc_call(
            rpc="get_worker_task_reachability",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.GetWorkerTaskReachabilityResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_worker_versioning_rules(
        self,
        req: temporalio.api.workflowservice.v1.GetWorkerVersioningRulesRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.GetWorkerVersioningRulesResponse:
        print("sup from get_worker_versioning_rules")
        return await self.client._rpc_call(
            rpc="get_worker_versioning_rules",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.GetWorkerVersioningRulesResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_workflow_execution_history(
        self,
        req: temporalio.api.workflowservice.v1.GetWorkflowExecutionHistoryRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.GetWorkflowExecutionHistoryResponse:
        print("sup from get_workflow_execution_history")
        return await self.client._rpc_call(
            rpc="get_workflow_execution_history",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.GetWorkflowExecutionHistoryResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_workflow_execution_history_reverse(
        self,
        req: temporalio.api.workflowservice.v1.GetWorkflowExecutionHistoryReverseRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.GetWorkflowExecutionHistoryReverseResponse:
        print("sup from get_workflow_execution_history_reverse")
        return await self.client._rpc_call(
            rpc="get_workflow_execution_history_reverse",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.GetWorkflowExecutionHistoryReverseResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def list_archived_workflow_executions(
        self,
        req: temporalio.api.workflowservice.v1.ListArchivedWorkflowExecutionsRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.ListArchivedWorkflowExecutionsResponse:
        print("sup from list_archived_workflow_executions")
        return await self.client._rpc_call(
            rpc="list_archived_workflow_executions",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.ListArchivedWorkflowExecutionsResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def list_batch_operations(
        self,
        req: temporalio.api.workflowservice.v1.ListBatchOperationsRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.ListBatchOperationsResponse:
        print("sup from list_batch_operations")
        return await self.client._rpc_call(
            rpc="list_batch_operations",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.ListBatchOperationsResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def list_closed_workflow_executions(
        self,
        req: temporalio.api.workflowservice.v1.ListClosedWorkflowExecutionsRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.ListClosedWorkflowExecutionsResponse:
        print("sup from list_closed_workflow_executions")
        return await self.client._rpc_call(
            rpc="list_closed_workflow_executions",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.ListClosedWorkflowExecutionsResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def list_deployments(
        self,
        req: temporalio.api.workflowservice.v1.ListDeploymentsRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.ListDeploymentsResponse:
        print("sup from list_deployments")
        return await self.client._rpc_call(
            rpc="list_deployments",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.ListDeploymentsResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def list_namespaces(
        self,
        req: temporalio.api.workflowservice.v1.ListNamespacesRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.ListNamespacesResponse:
        print("sup from list_namespaces")
        return await self.client._rpc_call(
            rpc="list_namespaces",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.ListNamespacesResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def list_open_workflow_executions(
        self,
        req: temporalio.api.workflowservice.v1.ListOpenWorkflowExecutionsRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.ListOpenWorkflowExecutionsResponse:
        print("sup from list_open_workflow_executions")
        return await self.client._rpc_call(
            rpc="list_open_workflow_executions",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.ListOpenWorkflowExecutionsResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def list_schedule_matching_times(
        self,
        req: temporalio.api.workflowservice.v1.ListScheduleMatchingTimesRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.ListScheduleMatchingTimesResponse:
        print("sup from list_schedule_matching_times")
        return await self.client._rpc_call(
            rpc="list_schedule_matching_times",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.ListScheduleMatchingTimesResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def list_schedules(
        self,
        req: temporalio.api.workflowservice.v1.ListSchedulesRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.ListSchedulesResponse:
        print("sup from list_schedules")
        return await self.client._rpc_call(
            rpc="list_schedules",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.ListSchedulesResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def list_task_queue_partitions(
        self,
        req: temporalio.api.workflowservice.v1.ListTaskQueuePartitionsRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.ListTaskQueuePartitionsResponse:
        print("sup from list_task_queue_partitions")
        return await self.client._rpc_call(
            rpc="list_task_queue_partitions",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.ListTaskQueuePartitionsResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def list_worker_deployments(
        self,
        req: temporalio.api.workflowservice.v1.ListWorkerDeploymentsRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.ListWorkerDeploymentsResponse:
        print("sup from list_worker_deployments")
        return await self.client._rpc_call(
            rpc="list_worker_deployments",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.ListWorkerDeploymentsResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def list_workers(
        self,
        req: temporalio.api.workflowservice.v1.ListWorkersRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.ListWorkersResponse:
        print("sup from list_workers")
        return await self.client._rpc_call(
            rpc="list_workers",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.ListWorkersResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def list_workflow_executions(
        self,
        req: temporalio.api.workflowservice.v1.ListWorkflowExecutionsRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.ListWorkflowExecutionsResponse:
        print("sup from list_workflow_executions")
        return await self.client._rpc_call(
            rpc="list_workflow_executions",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.ListWorkflowExecutionsResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def list_workflow_rules(
        self,
        req: temporalio.api.workflowservice.v1.ListWorkflowRulesRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.ListWorkflowRulesResponse:
        print("sup from list_workflow_rules")
        return await self.client._rpc_call(
            rpc="list_workflow_rules",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.ListWorkflowRulesResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def patch_schedule(
        self,
        req: temporalio.api.workflowservice.v1.PatchScheduleRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.PatchScheduleResponse:
        print("sup from patch_schedule")
        return await self.client._rpc_call(
            rpc="patch_schedule",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.PatchScheduleResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def pause_activity(
        self,
        req: temporalio.api.workflowservice.v1.PauseActivityRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.PauseActivityResponse:
        print("sup from pause_activity")
        return await self.client._rpc_call(
            rpc="pause_activity",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.PauseActivityResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def poll_activity_task_queue(
        self,
        req: temporalio.api.workflowservice.v1.PollActivityTaskQueueRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.PollActivityTaskQueueResponse:
        print("sup from poll_activity_task_queue")
        return await self.client._rpc_call(
            rpc="poll_activity_task_queue",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.PollActivityTaskQueueResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def poll_nexus_task_queue(
        self,
        req: temporalio.api.workflowservice.v1.PollNexusTaskQueueRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.PollNexusTaskQueueResponse:
        print("sup from poll_nexus_task_queue")
        return await self.client._rpc_call(
            rpc="poll_nexus_task_queue",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.PollNexusTaskQueueResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def poll_workflow_execution_update(
        self,
        req: temporalio.api.workflowservice.v1.PollWorkflowExecutionUpdateRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.PollWorkflowExecutionUpdateResponse:
        print("sup from poll_workflow_execution_update")
        return await self.client._rpc_call(
            rpc="poll_workflow_execution_update",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.PollWorkflowExecutionUpdateResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def poll_workflow_task_queue(
        self,
        req: temporalio.api.workflowservice.v1.PollWorkflowTaskQueueRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.PollWorkflowTaskQueueResponse:
        print("sup from poll_workflow_task_queue")
        return await self.client._rpc_call(
            rpc="poll_workflow_task_queue",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.PollWorkflowTaskQueueResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def query_workflow(
        self,
        req: temporalio.api.workflowservice.v1.QueryWorkflowRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.QueryWorkflowResponse:
        print("sup from query_workflow")
        return await self.client._rpc_call(
            rpc="query_workflow",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.QueryWorkflowResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def record_activity_task_heartbeat(
        self,
        req: temporalio.api.workflowservice.v1.RecordActivityTaskHeartbeatRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.RecordActivityTaskHeartbeatResponse:
        print("sup from record_activity_task_heartbeat")
        return await self.client._rpc_call(
            rpc="record_activity_task_heartbeat",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.RecordActivityTaskHeartbeatResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def record_activity_task_heartbeat_by_id(
        self,
        req: temporalio.api.workflowservice.v1.RecordActivityTaskHeartbeatByIdRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.RecordActivityTaskHeartbeatByIdResponse:
        print("sup from record_activity_task_heartbeat_by_id")
        return await self.client._rpc_call(
            rpc="record_activity_task_heartbeat_by_id",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.RecordActivityTaskHeartbeatByIdResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def record_worker_heartbeat(
        self,
        req: temporalio.api.workflowservice.v1.RecordWorkerHeartbeatRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.RecordWorkerHeartbeatResponse:
        print("sup from record_worker_heartbeat")
        return await self.client._rpc_call(
            rpc="record_worker_heartbeat",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.RecordWorkerHeartbeatResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def register_namespace(
        self,
        req: temporalio.api.workflowservice.v1.RegisterNamespaceRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.RegisterNamespaceResponse:
        print("sup from register_namespace")
        return await self.client._rpc_call(
            rpc="register_namespace",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.RegisterNamespaceResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def request_cancel_workflow_execution(
        self,
        req: temporalio.api.workflowservice.v1.RequestCancelWorkflowExecutionRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.RequestCancelWorkflowExecutionResponse:
        print("sup from request_cancel_workflow_execution")
        return await self.client._rpc_call(
            rpc="request_cancel_workflow_execution",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.RequestCancelWorkflowExecutionResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def reset_activity(
        self,
        req: temporalio.api.workflowservice.v1.ResetActivityRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.ResetActivityResponse:
        print("sup from reset_activity")
        return await self.client._rpc_call(
            rpc="reset_activity",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.ResetActivityResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def reset_sticky_task_queue(
        self,
        req: temporalio.api.workflowservice.v1.ResetStickyTaskQueueRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.ResetStickyTaskQueueResponse:
        print("sup from reset_sticky_task_queue")
        return await self.client._rpc_call(
            rpc="reset_sticky_task_queue",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.ResetStickyTaskQueueResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def reset_workflow_execution(
        self,
        req: temporalio.api.workflowservice.v1.ResetWorkflowExecutionRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.ResetWorkflowExecutionResponse:
        print("sup from reset_workflow_execution")
        return await self.client._rpc_call(
            rpc="reset_workflow_execution",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.ResetWorkflowExecutionResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def respond_activity_task_canceled(
        self,
        req: temporalio.api.workflowservice.v1.RespondActivityTaskCanceledRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.RespondActivityTaskCanceledResponse:
        print("sup from respond_activity_task_canceled")
        return await self.client._rpc_call(
            rpc="respond_activity_task_canceled",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.RespondActivityTaskCanceledResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def respond_activity_task_canceled_by_id(
        self,
        req: temporalio.api.workflowservice.v1.RespondActivityTaskCanceledByIdRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.RespondActivityTaskCanceledByIdResponse:
        print("sup from respond_activity_task_canceled_by_id")
        return await self.client._rpc_call(
            rpc="respond_activity_task_canceled_by_id",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.RespondActivityTaskCanceledByIdResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def respond_activity_task_completed(
        self,
        req: temporalio.api.workflowservice.v1.RespondActivityTaskCompletedRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.RespondActivityTaskCompletedResponse:
        print("sup from respond_activity_task_completed")
        return await self.client._rpc_call(
            rpc="respond_activity_task_completed",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.RespondActivityTaskCompletedResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def respond_activity_task_completed_by_id(
        self,
        req: temporalio.api.workflowservice.v1.RespondActivityTaskCompletedByIdRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.RespondActivityTaskCompletedByIdResponse:
        print("sup from respond_activity_task_completed_by_id")
        return await self.client._rpc_call(
            rpc="respond_activity_task_completed_by_id",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.RespondActivityTaskCompletedByIdResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def respond_activity_task_failed(
        self,
        req: temporalio.api.workflowservice.v1.RespondActivityTaskFailedRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.RespondActivityTaskFailedResponse:
        print("sup from respond_activity_task_failed")
        return await self.client._rpc_call(
            rpc="respond_activity_task_failed",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.RespondActivityTaskFailedResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def respond_activity_task_failed_by_id(
        self,
        req: temporalio.api.workflowservice.v1.RespondActivityTaskFailedByIdRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.RespondActivityTaskFailedByIdResponse:
        print("sup from respond_activity_task_failed_by_id")
        return await self.client._rpc_call(
            rpc="respond_activity_task_failed_by_id",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.RespondActivityTaskFailedByIdResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def respond_nexus_task_completed(
        self,
        req: temporalio.api.workflowservice.v1.RespondNexusTaskCompletedRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.RespondNexusTaskCompletedResponse:
        print("sup from respond_nexus_task_completed")
        return await self.client._rpc_call(
            rpc="respond_nexus_task_completed",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.RespondNexusTaskCompletedResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def respond_nexus_task_failed(
        self,
        req: temporalio.api.workflowservice.v1.RespondNexusTaskFailedRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.RespondNexusTaskFailedResponse:
        print("sup from respond_nexus_task_failed")
        return await self.client._rpc_call(
            rpc="respond_nexus_task_failed",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.RespondNexusTaskFailedResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def respond_query_task_completed(
        self,
        req: temporalio.api.workflowservice.v1.RespondQueryTaskCompletedRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.RespondQueryTaskCompletedResponse:
        print("sup from respond_query_task_completed")
        return await self.client._rpc_call(
            rpc="respond_query_task_completed",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.RespondQueryTaskCompletedResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def respond_workflow_task_completed(
        self,
        req: temporalio.api.workflowservice.v1.RespondWorkflowTaskCompletedRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.RespondWorkflowTaskCompletedResponse:
        print("sup from respond_workflow_task_completed")
        return await self.client._rpc_call(
            rpc="respond_workflow_task_completed",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.RespondWorkflowTaskCompletedResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def respond_workflow_task_failed(
        self,
        req: temporalio.api.workflowservice.v1.RespondWorkflowTaskFailedRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.RespondWorkflowTaskFailedResponse:
        print("sup from respond_workflow_task_failed")
        return await self.client._rpc_call(
            rpc="respond_workflow_task_failed",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.RespondWorkflowTaskFailedResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def scan_workflow_executions(
        self,
        req: temporalio.api.workflowservice.v1.ScanWorkflowExecutionsRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.ScanWorkflowExecutionsResponse:
        print("sup from scan_workflow_executions")
        return await self.client._rpc_call(
            rpc="scan_workflow_executions",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.ScanWorkflowExecutionsResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def set_current_deployment(
        self,
        req: temporalio.api.workflowservice.v1.SetCurrentDeploymentRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.SetCurrentDeploymentResponse:
        print("sup from set_current_deployment")
        return await self.client._rpc_call(
            rpc="set_current_deployment",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.SetCurrentDeploymentResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def set_worker_deployment_current_version(
        self,
        req: temporalio.api.workflowservice.v1.SetWorkerDeploymentCurrentVersionRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.SetWorkerDeploymentCurrentVersionResponse:
        print("sup from set_worker_deployment_current_version")
        return await self.client._rpc_call(
            rpc="set_worker_deployment_current_version",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.SetWorkerDeploymentCurrentVersionResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def set_worker_deployment_ramping_version(
        self,
        req: temporalio.api.workflowservice.v1.SetWorkerDeploymentRampingVersionRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.SetWorkerDeploymentRampingVersionResponse:
        print("sup from set_worker_deployment_ramping_version")
        return await self.client._rpc_call(
            rpc="set_worker_deployment_ramping_version",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.SetWorkerDeploymentRampingVersionResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def shutdown_worker(
        self,
        req: temporalio.api.workflowservice.v1.ShutdownWorkerRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.ShutdownWorkerResponse:
        print("sup from shutdown_worker")
        return await self.client._rpc_call(
            rpc="shutdown_worker",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.ShutdownWorkerResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def signal_with_start_workflow_execution(
        self,
        req: temporalio.api.workflowservice.v1.SignalWithStartWorkflowExecutionRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.SignalWithStartWorkflowExecutionResponse:
        print("sup from signal_with_start_workflow_execution")
        return await self.client._rpc_call(
            rpc="signal_with_start_workflow_execution",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.SignalWithStartWorkflowExecutionResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def signal_workflow_execution(
        self,
        req: temporalio.api.workflowservice.v1.SignalWorkflowExecutionRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.SignalWorkflowExecutionResponse:
        print("sup from signal_workflow_execution")
        return await self.client._rpc_call(
            rpc="signal_workflow_execution",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.SignalWorkflowExecutionResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def start_batch_operation(
        self,
        req: temporalio.api.workflowservice.v1.StartBatchOperationRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.StartBatchOperationResponse:
        print("sup from start_batch_operation")
        return await self.client._rpc_call(
            rpc="start_batch_operation",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.StartBatchOperationResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def start_workflow_execution(
        self,
        req: temporalio.api.workflowservice.v1.StartWorkflowExecutionRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.StartWorkflowExecutionResponse:
        print("sup from start_workflow_execution")
        return await self.client._rpc_call(
            rpc="start_workflow_execution",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.StartWorkflowExecutionResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def stop_batch_operation(
        self,
        req: temporalio.api.workflowservice.v1.StopBatchOperationRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.StopBatchOperationResponse:
        print("sup from stop_batch_operation")
        return await self.client._rpc_call(
            rpc="stop_batch_operation",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.StopBatchOperationResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def terminate_workflow_execution(
        self,
        req: temporalio.api.workflowservice.v1.TerminateWorkflowExecutionRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.TerminateWorkflowExecutionResponse:
        print("sup from terminate_workflow_execution")
        return await self.client._rpc_call(
            rpc="terminate_workflow_execution",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.TerminateWorkflowExecutionResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def trigger_workflow_rule(
        self,
        req: temporalio.api.workflowservice.v1.TriggerWorkflowRuleRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.TriggerWorkflowRuleResponse:
        print("sup from trigger_workflow_rule")
        return await self.client._rpc_call(
            rpc="trigger_workflow_rule",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.TriggerWorkflowRuleResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def unpause_activity(
        self,
        req: temporalio.api.workflowservice.v1.UnpauseActivityRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.UnpauseActivityResponse:
        print("sup from unpause_activity")
        return await self.client._rpc_call(
            rpc="unpause_activity",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.UnpauseActivityResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def update_activity_options(
        self,
        req: temporalio.api.workflowservice.v1.UpdateActivityOptionsRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.UpdateActivityOptionsResponse:
        print("sup from update_activity_options")
        return await self.client._rpc_call(
            rpc="update_activity_options",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.UpdateActivityOptionsResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def update_namespace(
        self,
        req: temporalio.api.workflowservice.v1.UpdateNamespaceRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.UpdateNamespaceResponse:
        print("sup from update_namespace")
        return await self.client._rpc_call(
            rpc="update_namespace",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.UpdateNamespaceResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def update_schedule(
        self,
        req: temporalio.api.workflowservice.v1.UpdateScheduleRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.UpdateScheduleResponse:
        print("sup from update_schedule")
        return await self.client._rpc_call(
            rpc="update_schedule",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.UpdateScheduleResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def update_task_queue_config(
        self,
        req: temporalio.api.workflowservice.v1.UpdateTaskQueueConfigRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.UpdateTaskQueueConfigResponse:
        print("sup from update_task_queue_config")
        return await self.client._rpc_call(
            rpc="update_task_queue_config",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.UpdateTaskQueueConfigResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def update_worker_build_id_compatibility(
        self,
        req: temporalio.api.workflowservice.v1.UpdateWorkerBuildIdCompatibilityRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.UpdateWorkerBuildIdCompatibilityResponse:
        print("sup from update_worker_build_id_compatibility")
        return await self.client._rpc_call(
            rpc="update_worker_build_id_compatibility",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.UpdateWorkerBuildIdCompatibilityResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def update_worker_config(
        self,
        req: temporalio.api.workflowservice.v1.UpdateWorkerConfigRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.UpdateWorkerConfigResponse:
        print("sup from update_worker_config")
        return await self.client._rpc_call(
            rpc="update_worker_config",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.UpdateWorkerConfigResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def update_worker_deployment_version_metadata(
        self,
        req: temporalio.api.workflowservice.v1.UpdateWorkerDeploymentVersionMetadataRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.UpdateWorkerDeploymentVersionMetadataResponse:
        print("sup from update_worker_deployment_version_metadata")
        return await self.client._rpc_call(
            rpc="update_worker_deployment_version_metadata",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.UpdateWorkerDeploymentVersionMetadataResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def update_worker_versioning_rules(
        self,
        req: temporalio.api.workflowservice.v1.UpdateWorkerVersioningRulesRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.UpdateWorkerVersioningRulesResponse:
        print("sup from update_worker_versioning_rules")
        return await self.client._rpc_call(
            rpc="update_worker_versioning_rules",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.UpdateWorkerVersioningRulesResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def update_workflow_execution(
        self,
        req: temporalio.api.workflowservice.v1.UpdateWorkflowExecutionRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.UpdateWorkflowExecutionResponse:
        print("sup from update_workflow_execution")
        return await self.client._rpc_call(
            rpc="update_workflow_execution",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.UpdateWorkflowExecutionResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def update_workflow_execution_options(
        self,
        req: temporalio.api.workflowservice.v1.UpdateWorkflowExecutionOptionsRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.workflowservice.v1.UpdateWorkflowExecutionOptionsResponse:
        print("sup from update_workflow_execution_options")
        return await self.client._rpc_call(
            rpc="update_workflow_execution_options",
            req=req,
            service=self.service,
            resp_type=temporalio.api.workflowservice.v1.UpdateWorkflowExecutionOptionsResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )



class OperatorService:
    def __init__(self, client: ServiceClient):
        self.client = client
        self.service = "operator"

    async def add_or_update_remote_cluster(
        self,
        req: temporalio.api.operatorservice.v1.AddOrUpdateRemoteClusterRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.operatorservice.v1.AddOrUpdateRemoteClusterResponse:
        print("sup from add_or_update_remote_cluster")
        return await self.client._rpc_call(
            rpc="add_or_update_remote_cluster",
            req=req,
            service=self.service,
            resp_type=temporalio.api.operatorservice.v1.AddOrUpdateRemoteClusterResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def add_search_attributes(
        self,
        req: temporalio.api.operatorservice.v1.AddSearchAttributesRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.operatorservice.v1.AddSearchAttributesResponse:
        print("sup from add_search_attributes")
        return await self.client._rpc_call(
            rpc="add_search_attributes",
            req=req,
            service=self.service,
            resp_type=temporalio.api.operatorservice.v1.AddSearchAttributesResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def create_nexus_endpoint(
        self,
        req: temporalio.api.operatorservice.v1.CreateNexusEndpointRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.operatorservice.v1.CreateNexusEndpointResponse:
        print("sup from create_nexus_endpoint")
        return await self.client._rpc_call(
            rpc="create_nexus_endpoint",
            req=req,
            service=self.service,
            resp_type=temporalio.api.operatorservice.v1.CreateNexusEndpointResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def delete_namespace(
        self,
        req: temporalio.api.operatorservice.v1.DeleteNamespaceRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.operatorservice.v1.DeleteNamespaceResponse:
        print("sup from delete_namespace")
        return await self.client._rpc_call(
            rpc="delete_namespace",
            req=req,
            service=self.service,
            resp_type=temporalio.api.operatorservice.v1.DeleteNamespaceResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def delete_nexus_endpoint(
        self,
        req: temporalio.api.operatorservice.v1.DeleteNexusEndpointRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.operatorservice.v1.DeleteNexusEndpointResponse:
        print("sup from delete_nexus_endpoint")
        return await self.client._rpc_call(
            rpc="delete_nexus_endpoint",
            req=req,
            service=self.service,
            resp_type=temporalio.api.operatorservice.v1.DeleteNexusEndpointResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_nexus_endpoint(
        self,
        req: temporalio.api.operatorservice.v1.GetNexusEndpointRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.operatorservice.v1.GetNexusEndpointResponse:
        print("sup from get_nexus_endpoint")
        return await self.client._rpc_call(
            rpc="get_nexus_endpoint",
            req=req,
            service=self.service,
            resp_type=temporalio.api.operatorservice.v1.GetNexusEndpointResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def list_clusters(
        self,
        req: temporalio.api.operatorservice.v1.ListClustersRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.operatorservice.v1.ListClustersResponse:
        print("sup from list_clusters")
        return await self.client._rpc_call(
            rpc="list_clusters",
            req=req,
            service=self.service,
            resp_type=temporalio.api.operatorservice.v1.ListClustersResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def list_nexus_endpoints(
        self,
        req: temporalio.api.operatorservice.v1.ListNexusEndpointsRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.operatorservice.v1.ListNexusEndpointsResponse:
        print("sup from list_nexus_endpoints")
        return await self.client._rpc_call(
            rpc="list_nexus_endpoints",
            req=req,
            service=self.service,
            resp_type=temporalio.api.operatorservice.v1.ListNexusEndpointsResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def list_search_attributes(
        self,
        req: temporalio.api.operatorservice.v1.ListSearchAttributesRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.operatorservice.v1.ListSearchAttributesResponse:
        print("sup from list_search_attributes")
        return await self.client._rpc_call(
            rpc="list_search_attributes",
            req=req,
            service=self.service,
            resp_type=temporalio.api.operatorservice.v1.ListSearchAttributesResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def remove_remote_cluster(
        self,
        req: temporalio.api.operatorservice.v1.RemoveRemoteClusterRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.operatorservice.v1.RemoveRemoteClusterResponse:
        print("sup from remove_remote_cluster")
        return await self.client._rpc_call(
            rpc="remove_remote_cluster",
            req=req,
            service=self.service,
            resp_type=temporalio.api.operatorservice.v1.RemoveRemoteClusterResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def remove_search_attributes(
        self,
        req: temporalio.api.operatorservice.v1.RemoveSearchAttributesRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.operatorservice.v1.RemoveSearchAttributesResponse:
        print("sup from remove_search_attributes")
        return await self.client._rpc_call(
            rpc="remove_search_attributes",
            req=req,
            service=self.service,
            resp_type=temporalio.api.operatorservice.v1.RemoveSearchAttributesResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def update_nexus_endpoint(
        self,
        req: temporalio.api.operatorservice.v1.UpdateNexusEndpointRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.operatorservice.v1.UpdateNexusEndpointResponse:
        print("sup from update_nexus_endpoint")
        return await self.client._rpc_call(
            rpc="update_nexus_endpoint",
            req=req,
            service=self.service,
            resp_type=temporalio.api.operatorservice.v1.UpdateNexusEndpointResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )



class CloudService:
    def __init__(self, client: ServiceClient):
        self.client = client
        self.service = "cloud"

    async def add_namespace_region(
        self,
        req: temporalio.api.cloud.cloudservice.v1.AddNamespaceRegionRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.AddNamespaceRegionResponse:
        print("sup from add_namespace_region")
        return await self.client._rpc_call(
            rpc="add_namespace_region",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.AddNamespaceRegionResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def add_user_group_member(
        self,
        req: temporalio.api.cloud.cloudservice.v1.AddUserGroupMemberRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.AddUserGroupMemberResponse:
        print("sup from add_user_group_member")
        return await self.client._rpc_call(
            rpc="add_user_group_member",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.AddUserGroupMemberResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def create_api_key(
        self,
        req: temporalio.api.cloud.cloudservice.v1.CreateApiKeyRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.CreateApiKeyResponse:
        print("sup from create_api_key")
        return await self.client._rpc_call(
            rpc="create_api_key",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.CreateApiKeyResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def create_connectivity_rule(
        self,
        req: temporalio.api.cloud.cloudservice.v1.CreateConnectivityRuleRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.CreateConnectivityRuleResponse:
        print("sup from create_connectivity_rule")
        return await self.client._rpc_call(
            rpc="create_connectivity_rule",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.CreateConnectivityRuleResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def create_namespace(
        self,
        req: temporalio.api.cloud.cloudservice.v1.CreateNamespaceRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.CreateNamespaceResponse:
        print("sup from create_namespace")
        return await self.client._rpc_call(
            rpc="create_namespace",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.CreateNamespaceResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def create_namespace_export_sink(
        self,
        req: temporalio.api.cloud.cloudservice.v1.CreateNamespaceExportSinkRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.CreateNamespaceExportSinkResponse:
        print("sup from create_namespace_export_sink")
        return await self.client._rpc_call(
            rpc="create_namespace_export_sink",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.CreateNamespaceExportSinkResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def create_nexus_endpoint(
        self,
        req: temporalio.api.cloud.cloudservice.v1.CreateNexusEndpointRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.CreateNexusEndpointResponse:
        print("sup from create_nexus_endpoint")
        return await self.client._rpc_call(
            rpc="create_nexus_endpoint",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.CreateNexusEndpointResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def create_service_account(
        self,
        req: temporalio.api.cloud.cloudservice.v1.CreateServiceAccountRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.CreateServiceAccountResponse:
        print("sup from create_service_account")
        return await self.client._rpc_call(
            rpc="create_service_account",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.CreateServiceAccountResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def create_user(
        self,
        req: temporalio.api.cloud.cloudservice.v1.CreateUserRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.CreateUserResponse:
        print("sup from create_user")
        return await self.client._rpc_call(
            rpc="create_user",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.CreateUserResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def create_user_group(
        self,
        req: temporalio.api.cloud.cloudservice.v1.CreateUserGroupRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.CreateUserGroupResponse:
        print("sup from create_user_group")
        return await self.client._rpc_call(
            rpc="create_user_group",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.CreateUserGroupResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def delete_api_key(
        self,
        req: temporalio.api.cloud.cloudservice.v1.DeleteApiKeyRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.DeleteApiKeyResponse:
        print("sup from delete_api_key")
        return await self.client._rpc_call(
            rpc="delete_api_key",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.DeleteApiKeyResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def delete_connectivity_rule(
        self,
        req: temporalio.api.cloud.cloudservice.v1.DeleteConnectivityRuleRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.DeleteConnectivityRuleResponse:
        print("sup from delete_connectivity_rule")
        return await self.client._rpc_call(
            rpc="delete_connectivity_rule",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.DeleteConnectivityRuleResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def delete_namespace(
        self,
        req: temporalio.api.cloud.cloudservice.v1.DeleteNamespaceRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.DeleteNamespaceResponse:
        print("sup from delete_namespace")
        return await self.client._rpc_call(
            rpc="delete_namespace",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.DeleteNamespaceResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def delete_namespace_export_sink(
        self,
        req: temporalio.api.cloud.cloudservice.v1.DeleteNamespaceExportSinkRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.DeleteNamespaceExportSinkResponse:
        print("sup from delete_namespace_export_sink")
        return await self.client._rpc_call(
            rpc="delete_namespace_export_sink",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.DeleteNamespaceExportSinkResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def delete_namespace_region(
        self,
        req: temporalio.api.cloud.cloudservice.v1.DeleteNamespaceRegionRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.DeleteNamespaceRegionResponse:
        print("sup from delete_namespace_region")
        return await self.client._rpc_call(
            rpc="delete_namespace_region",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.DeleteNamespaceRegionResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def delete_nexus_endpoint(
        self,
        req: temporalio.api.cloud.cloudservice.v1.DeleteNexusEndpointRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.DeleteNexusEndpointResponse:
        print("sup from delete_nexus_endpoint")
        return await self.client._rpc_call(
            rpc="delete_nexus_endpoint",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.DeleteNexusEndpointResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def delete_service_account(
        self,
        req: temporalio.api.cloud.cloudservice.v1.DeleteServiceAccountRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.DeleteServiceAccountResponse:
        print("sup from delete_service_account")
        return await self.client._rpc_call(
            rpc="delete_service_account",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.DeleteServiceAccountResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def delete_user(
        self,
        req: temporalio.api.cloud.cloudservice.v1.DeleteUserRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.DeleteUserResponse:
        print("sup from delete_user")
        return await self.client._rpc_call(
            rpc="delete_user",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.DeleteUserResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def delete_user_group(
        self,
        req: temporalio.api.cloud.cloudservice.v1.DeleteUserGroupRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.DeleteUserGroupResponse:
        print("sup from delete_user_group")
        return await self.client._rpc_call(
            rpc="delete_user_group",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.DeleteUserGroupResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def failover_namespace_region(
        self,
        req: temporalio.api.cloud.cloudservice.v1.FailoverNamespaceRegionRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.FailoverNamespaceRegionResponse:
        print("sup from failover_namespace_region")
        return await self.client._rpc_call(
            rpc="failover_namespace_region",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.FailoverNamespaceRegionResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_account(
        self,
        req: temporalio.api.cloud.cloudservice.v1.GetAccountRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.GetAccountResponse:
        print("sup from get_account")
        return await self.client._rpc_call(
            rpc="get_account",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.GetAccountResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_api_key(
        self,
        req: temporalio.api.cloud.cloudservice.v1.GetApiKeyRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.GetApiKeyResponse:
        print("sup from get_api_key")
        return await self.client._rpc_call(
            rpc="get_api_key",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.GetApiKeyResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_api_keys(
        self,
        req: temporalio.api.cloud.cloudservice.v1.GetApiKeysRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.GetApiKeysResponse:
        print("sup from get_api_keys")
        return await self.client._rpc_call(
            rpc="get_api_keys",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.GetApiKeysResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_async_operation(
        self,
        req: temporalio.api.cloud.cloudservice.v1.GetAsyncOperationRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.GetAsyncOperationResponse:
        print("sup from get_async_operation")
        return await self.client._rpc_call(
            rpc="get_async_operation",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.GetAsyncOperationResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_connectivity_rule(
        self,
        req: temporalio.api.cloud.cloudservice.v1.GetConnectivityRuleRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.GetConnectivityRuleResponse:
        print("sup from get_connectivity_rule")
        return await self.client._rpc_call(
            rpc="get_connectivity_rule",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.GetConnectivityRuleResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_connectivity_rules(
        self,
        req: temporalio.api.cloud.cloudservice.v1.GetConnectivityRulesRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.GetConnectivityRulesResponse:
        print("sup from get_connectivity_rules")
        return await self.client._rpc_call(
            rpc="get_connectivity_rules",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.GetConnectivityRulesResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_namespace(
        self,
        req: temporalio.api.cloud.cloudservice.v1.GetNamespaceRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.GetNamespaceResponse:
        print("sup from get_namespace")
        return await self.client._rpc_call(
            rpc="get_namespace",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.GetNamespaceResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_namespace_export_sink(
        self,
        req: temporalio.api.cloud.cloudservice.v1.GetNamespaceExportSinkRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.GetNamespaceExportSinkResponse:
        print("sup from get_namespace_export_sink")
        return await self.client._rpc_call(
            rpc="get_namespace_export_sink",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.GetNamespaceExportSinkResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_namespace_export_sinks(
        self,
        req: temporalio.api.cloud.cloudservice.v1.GetNamespaceExportSinksRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.GetNamespaceExportSinksResponse:
        print("sup from get_namespace_export_sinks")
        return await self.client._rpc_call(
            rpc="get_namespace_export_sinks",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.GetNamespaceExportSinksResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_namespaces(
        self,
        req: temporalio.api.cloud.cloudservice.v1.GetNamespacesRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.GetNamespacesResponse:
        print("sup from get_namespaces")
        return await self.client._rpc_call(
            rpc="get_namespaces",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.GetNamespacesResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_nexus_endpoint(
        self,
        req: temporalio.api.cloud.cloudservice.v1.GetNexusEndpointRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.GetNexusEndpointResponse:
        print("sup from get_nexus_endpoint")
        return await self.client._rpc_call(
            rpc="get_nexus_endpoint",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.GetNexusEndpointResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_nexus_endpoints(
        self,
        req: temporalio.api.cloud.cloudservice.v1.GetNexusEndpointsRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.GetNexusEndpointsResponse:
        print("sup from get_nexus_endpoints")
        return await self.client._rpc_call(
            rpc="get_nexus_endpoints",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.GetNexusEndpointsResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_region(
        self,
        req: temporalio.api.cloud.cloudservice.v1.GetRegionRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.GetRegionResponse:
        print("sup from get_region")
        return await self.client._rpc_call(
            rpc="get_region",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.GetRegionResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_regions(
        self,
        req: temporalio.api.cloud.cloudservice.v1.GetRegionsRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.GetRegionsResponse:
        print("sup from get_regions")
        return await self.client._rpc_call(
            rpc="get_regions",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.GetRegionsResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_service_account(
        self,
        req: temporalio.api.cloud.cloudservice.v1.GetServiceAccountRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.GetServiceAccountResponse:
        print("sup from get_service_account")
        return await self.client._rpc_call(
            rpc="get_service_account",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.GetServiceAccountResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_service_accounts(
        self,
        req: temporalio.api.cloud.cloudservice.v1.GetServiceAccountsRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.GetServiceAccountsResponse:
        print("sup from get_service_accounts")
        return await self.client._rpc_call(
            rpc="get_service_accounts",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.GetServiceAccountsResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_usage(
        self,
        req: temporalio.api.cloud.cloudservice.v1.GetUsageRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.GetUsageResponse:
        print("sup from get_usage")
        return await self.client._rpc_call(
            rpc="get_usage",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.GetUsageResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_user(
        self,
        req: temporalio.api.cloud.cloudservice.v1.GetUserRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.GetUserResponse:
        print("sup from get_user")
        return await self.client._rpc_call(
            rpc="get_user",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.GetUserResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_user_group(
        self,
        req: temporalio.api.cloud.cloudservice.v1.GetUserGroupRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.GetUserGroupResponse:
        print("sup from get_user_group")
        return await self.client._rpc_call(
            rpc="get_user_group",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.GetUserGroupResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_user_group_members(
        self,
        req: temporalio.api.cloud.cloudservice.v1.GetUserGroupMembersRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.GetUserGroupMembersResponse:
        print("sup from get_user_group_members")
        return await self.client._rpc_call(
            rpc="get_user_group_members",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.GetUserGroupMembersResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_user_groups(
        self,
        req: temporalio.api.cloud.cloudservice.v1.GetUserGroupsRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.GetUserGroupsResponse:
        print("sup from get_user_groups")
        return await self.client._rpc_call(
            rpc="get_user_groups",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.GetUserGroupsResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def get_users(
        self,
        req: temporalio.api.cloud.cloudservice.v1.GetUsersRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.GetUsersResponse:
        print("sup from get_users")
        return await self.client._rpc_call(
            rpc="get_users",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.GetUsersResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def remove_user_group_member(
        self,
        req: temporalio.api.cloud.cloudservice.v1.RemoveUserGroupMemberRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.RemoveUserGroupMemberResponse:
        print("sup from remove_user_group_member")
        return await self.client._rpc_call(
            rpc="remove_user_group_member",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.RemoveUserGroupMemberResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def rename_custom_search_attribute(
        self,
        req: temporalio.api.cloud.cloudservice.v1.RenameCustomSearchAttributeRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.RenameCustomSearchAttributeResponse:
        print("sup from rename_custom_search_attribute")
        return await self.client._rpc_call(
            rpc="rename_custom_search_attribute",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.RenameCustomSearchAttributeResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def set_user_group_namespace_access(
        self,
        req: temporalio.api.cloud.cloudservice.v1.SetUserGroupNamespaceAccessRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.SetUserGroupNamespaceAccessResponse:
        print("sup from set_user_group_namespace_access")
        return await self.client._rpc_call(
            rpc="set_user_group_namespace_access",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.SetUserGroupNamespaceAccessResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def set_user_namespace_access(
        self,
        req: temporalio.api.cloud.cloudservice.v1.SetUserNamespaceAccessRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.SetUserNamespaceAccessResponse:
        print("sup from set_user_namespace_access")
        return await self.client._rpc_call(
            rpc="set_user_namespace_access",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.SetUserNamespaceAccessResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def update_account(
        self,
        req: temporalio.api.cloud.cloudservice.v1.UpdateAccountRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.UpdateAccountResponse:
        print("sup from update_account")
        return await self.client._rpc_call(
            rpc="update_account",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.UpdateAccountResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def update_api_key(
        self,
        req: temporalio.api.cloud.cloudservice.v1.UpdateApiKeyRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.UpdateApiKeyResponse:
        print("sup from update_api_key")
        return await self.client._rpc_call(
            rpc="update_api_key",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.UpdateApiKeyResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def update_namespace(
        self,
        req: temporalio.api.cloud.cloudservice.v1.UpdateNamespaceRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.UpdateNamespaceResponse:
        print("sup from update_namespace")
        return await self.client._rpc_call(
            rpc="update_namespace",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.UpdateNamespaceResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def update_namespace_export_sink(
        self,
        req: temporalio.api.cloud.cloudservice.v1.UpdateNamespaceExportSinkRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.UpdateNamespaceExportSinkResponse:
        print("sup from update_namespace_export_sink")
        return await self.client._rpc_call(
            rpc="update_namespace_export_sink",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.UpdateNamespaceExportSinkResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def update_namespace_tags(
        self,
        req: temporalio.api.cloud.cloudservice.v1.UpdateNamespaceTagsRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.UpdateNamespaceTagsResponse:
        print("sup from update_namespace_tags")
        return await self.client._rpc_call(
            rpc="update_namespace_tags",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.UpdateNamespaceTagsResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def update_nexus_endpoint(
        self,
        req: temporalio.api.cloud.cloudservice.v1.UpdateNexusEndpointRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.UpdateNexusEndpointResponse:
        print("sup from update_nexus_endpoint")
        return await self.client._rpc_call(
            rpc="update_nexus_endpoint",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.UpdateNexusEndpointResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def update_service_account(
        self,
        req: temporalio.api.cloud.cloudservice.v1.UpdateServiceAccountRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.UpdateServiceAccountResponse:
        print("sup from update_service_account")
        return await self.client._rpc_call(
            rpc="update_service_account",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.UpdateServiceAccountResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def update_user(
        self,
        req: temporalio.api.cloud.cloudservice.v1.UpdateUserRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.UpdateUserResponse:
        print("sup from update_user")
        return await self.client._rpc_call(
            rpc="update_user",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.UpdateUserResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def update_user_group(
        self,
        req: temporalio.api.cloud.cloudservice.v1.UpdateUserGroupRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.UpdateUserGroupResponse:
        print("sup from update_user_group")
        return await self.client._rpc_call(
            rpc="update_user_group",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.UpdateUserGroupResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def validate_namespace_export_sink(
        self,
        req: temporalio.api.cloud.cloudservice.v1.ValidateNamespaceExportSinkRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.cloud.cloudservice.v1.ValidateNamespaceExportSinkResponse:
        print("sup from validate_namespace_export_sink")
        return await self.client._rpc_call(
            rpc="validate_namespace_export_sink",
            req=req,
            service=self.service,
            resp_type=temporalio.api.cloud.cloudservice.v1.ValidateNamespaceExportSinkResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )



class TestService:
    def __init__(self, client: ServiceClient):
        self.client = client
        self.service = "test"

    async def get_current_time(
        self,
        req: google.protobuf.empty_pb2.Empty,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.testservice.v1.GetCurrentTimeResponse:
        print("sup from get_current_time")
        return await self.client._rpc_call(
            rpc="get_current_time",
            req=req,
            service=self.service,
            resp_type=temporalio.api.testservice.v1.GetCurrentTimeResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def lock_time_skipping(
        self,
        req: temporalio.api.testservice.v1.LockTimeSkippingRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.testservice.v1.LockTimeSkippingResponse:
        print("sup from lock_time_skipping")
        return await self.client._rpc_call(
            rpc="lock_time_skipping",
            req=req,
            service=self.service,
            resp_type=temporalio.api.testservice.v1.LockTimeSkippingResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def sleep(
        self,
        req: temporalio.api.testservice.v1.SleepRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.testservice.v1.SleepResponse:
        print("sup from sleep")
        return await self.client._rpc_call(
            rpc="sleep",
            req=req,
            service=self.service,
            resp_type=temporalio.api.testservice.v1.SleepResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def sleep_until(
        self,
        req: temporalio.api.testservice.v1.SleepUntilRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.testservice.v1.SleepResponse:
        print("sup from sleep_until")
        return await self.client._rpc_call(
            rpc="sleep_until",
            req=req,
            service=self.service,
            resp_type=temporalio.api.testservice.v1.SleepResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def unlock_time_skipping(
        self,
        req: temporalio.api.testservice.v1.UnlockTimeSkippingRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.testservice.v1.UnlockTimeSkippingResponse:
        print("sup from unlock_time_skipping")
        return await self.client._rpc_call(
            rpc="unlock_time_skipping",
            req=req,
            service=self.service,
            resp_type=temporalio.api.testservice.v1.UnlockTimeSkippingResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


    async def unlock_time_skipping_with_sleep(
        self,
        req: temporalio.api.testservice.v1.SleepRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.api.testservice.v1.SleepResponse:
        print("sup from unlock_time_skipping_with_sleep")
        return await self.client._rpc_call(
            rpc="unlock_time_skipping_with_sleep",
            req=req,
            service=self.service,
            resp_type=temporalio.api.testservice.v1.SleepResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )



class HealthService:
    def __init__(self, client: ServiceClient):
        self.client = client
        self.service = "health"

    async def check(
        self,
        req: temporalio.bridge.proto.health.v1.HealthCheckRequest,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> temporalio.bridge.proto.health.v1.HealthCheckResponse:
        print("sup from check")
        return await self.client._rpc_call(
            rpc="check",
            req=req,
            service=self.service,
            resp_type=temporalio.bridge.proto.health.v1.HealthCheckResponse,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )


