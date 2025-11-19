// Generated file. DO NOT EDIT

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use super::{
    client::{rpc_req, rpc_resp, ClientRef, RpcCall},
    rpc_call,
};

#[pymethods]
impl ClientRef {
    fn call_workflow_service<'p>(
        &self,
        py: Python<'p>,
        call: RpcCall,
    ) -> PyResult<Bound<'p, PyAny>> {
        self.runtime.assert_same_process("use client")?;
        use temporalio_client::WorkflowService;
        let mut retry_client = self.retry_client.clone();
        self.runtime.future_into_py(py, async move {
            let bytes = match call.rpc.as_str() {
                "count_workflow_executions" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        count_workflow_executions
                    )
                }
                "create_schedule" => {
                    rpc_call!(retry_client, call, WorkflowService, create_schedule)
                }
                "create_workflow_rule" => {
                    rpc_call!(retry_client, call, WorkflowService, create_workflow_rule)
                }
                "delete_schedule" => {
                    rpc_call!(retry_client, call, WorkflowService, delete_schedule)
                }
                "delete_worker_deployment" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        delete_worker_deployment
                    )
                }
                "delete_worker_deployment_version" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        delete_worker_deployment_version
                    )
                }
                "delete_workflow_execution" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        delete_workflow_execution
                    )
                }
                "delete_workflow_rule" => {
                    rpc_call!(retry_client, call, WorkflowService, delete_workflow_rule)
                }
                "deprecate_namespace" => {
                    rpc_call!(retry_client, call, WorkflowService, deprecate_namespace)
                }
                "describe_batch_operation" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        describe_batch_operation
                    )
                }
                "describe_deployment" => {
                    rpc_call!(retry_client, call, WorkflowService, describe_deployment)
                }
                "describe_namespace" => {
                    rpc_call!(retry_client, call, WorkflowService, describe_namespace)
                }
                "describe_schedule" => {
                    rpc_call!(retry_client, call, WorkflowService, describe_schedule)
                }
                "describe_task_queue" => {
                    rpc_call!(retry_client, call, WorkflowService, describe_task_queue)
                }
                "describe_worker" => {
                    rpc_call!(retry_client, call, WorkflowService, describe_worker)
                }
                "describe_worker_deployment" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        describe_worker_deployment
                    )
                }
                "describe_worker_deployment_version" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        describe_worker_deployment_version
                    )
                }
                "describe_workflow_execution" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        describe_workflow_execution
                    )
                }
                "describe_workflow_rule" => {
                    rpc_call!(retry_client, call, WorkflowService, describe_workflow_rule)
                }
                "execute_multi_operation" => {
                    rpc_call!(retry_client, call, WorkflowService, execute_multi_operation)
                }
                "fetch_worker_config" => {
                    rpc_call!(retry_client, call, WorkflowService, fetch_worker_config)
                }
                "get_cluster_info" => {
                    rpc_call!(retry_client, call, WorkflowService, get_cluster_info)
                }
                "get_current_deployment" => {
                    rpc_call!(retry_client, call, WorkflowService, get_current_deployment)
                }
                "get_deployment_reachability" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        get_deployment_reachability
                    )
                }
                "get_search_attributes" => {
                    rpc_call!(retry_client, call, WorkflowService, get_search_attributes)
                }
                "get_system_info" => {
                    rpc_call!(retry_client, call, WorkflowService, get_system_info)
                }
                "get_worker_build_id_compatibility" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        get_worker_build_id_compatibility
                    )
                }
                "get_worker_task_reachability" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        get_worker_task_reachability
                    )
                }
                "get_worker_versioning_rules" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        get_worker_versioning_rules
                    )
                }
                "get_workflow_execution_history" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        get_workflow_execution_history
                    )
                }
                "get_workflow_execution_history_reverse" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        get_workflow_execution_history_reverse
                    )
                }
                "list_archived_workflow_executions" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        list_archived_workflow_executions
                    )
                }
                "list_batch_operations" => {
                    rpc_call!(retry_client, call, WorkflowService, list_batch_operations)
                }
                "list_closed_workflow_executions" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        list_closed_workflow_executions
                    )
                }
                "list_deployments" => {
                    rpc_call!(retry_client, call, WorkflowService, list_deployments)
                }
                "list_namespaces" => {
                    rpc_call!(retry_client, call, WorkflowService, list_namespaces)
                }
                "list_open_workflow_executions" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        list_open_workflow_executions
                    )
                }
                "list_schedule_matching_times" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        list_schedule_matching_times
                    )
                }
                "list_schedules" => {
                    rpc_call!(retry_client, call, WorkflowService, list_schedules)
                }
                "list_task_queue_partitions" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        list_task_queue_partitions
                    )
                }
                "list_worker_deployments" => {
                    rpc_call!(retry_client, call, WorkflowService, list_worker_deployments)
                }
                "list_workers" => {
                    rpc_call!(retry_client, call, WorkflowService, list_workers)
                }
                "list_workflow_executions" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        list_workflow_executions
                    )
                }
                "list_workflow_rules" => {
                    rpc_call!(retry_client, call, WorkflowService, list_workflow_rules)
                }
                "patch_schedule" => {
                    rpc_call!(retry_client, call, WorkflowService, patch_schedule)
                }
                "pause_activity" => {
                    rpc_call!(retry_client, call, WorkflowService, pause_activity)
                }
                "poll_activity_task_queue" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        poll_activity_task_queue
                    )
                }
                "poll_nexus_task_queue" => {
                    rpc_call!(retry_client, call, WorkflowService, poll_nexus_task_queue)
                }
                "poll_workflow_execution_update" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        poll_workflow_execution_update
                    )
                }
                "poll_workflow_task_queue" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        poll_workflow_task_queue
                    )
                }
                "query_workflow" => {
                    rpc_call!(retry_client, call, WorkflowService, query_workflow)
                }
                "record_activity_task_heartbeat" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        record_activity_task_heartbeat
                    )
                }
                "record_activity_task_heartbeat_by_id" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        record_activity_task_heartbeat_by_id
                    )
                }
                "record_worker_heartbeat" => {
                    rpc_call!(retry_client, call, WorkflowService, record_worker_heartbeat)
                }
                "register_namespace" => {
                    rpc_call!(retry_client, call, WorkflowService, register_namespace)
                }
                "request_cancel_workflow_execution" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        request_cancel_workflow_execution
                    )
                }
                "reset_activity" => {
                    rpc_call!(retry_client, call, WorkflowService, reset_activity)
                }
                "reset_sticky_task_queue" => {
                    rpc_call!(retry_client, call, WorkflowService, reset_sticky_task_queue)
                }
                "reset_workflow_execution" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        reset_workflow_execution
                    )
                }
                "respond_activity_task_canceled" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        respond_activity_task_canceled
                    )
                }
                "respond_activity_task_canceled_by_id" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        respond_activity_task_canceled_by_id
                    )
                }
                "respond_activity_task_completed" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        respond_activity_task_completed
                    )
                }
                "respond_activity_task_completed_by_id" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        respond_activity_task_completed_by_id
                    )
                }
                "respond_activity_task_failed" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        respond_activity_task_failed
                    )
                }
                "respond_activity_task_failed_by_id" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        respond_activity_task_failed_by_id
                    )
                }
                "respond_nexus_task_completed" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        respond_nexus_task_completed
                    )
                }
                "respond_nexus_task_failed" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        respond_nexus_task_failed
                    )
                }
                "respond_query_task_completed" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        respond_query_task_completed
                    )
                }
                "respond_workflow_task_completed" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        respond_workflow_task_completed
                    )
                }
                "respond_workflow_task_failed" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        respond_workflow_task_failed
                    )
                }
                "scan_workflow_executions" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        scan_workflow_executions
                    )
                }
                "set_current_deployment" => {
                    rpc_call!(retry_client, call, WorkflowService, set_current_deployment)
                }
                "set_worker_deployment_current_version" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        set_worker_deployment_current_version
                    )
                }
                "set_worker_deployment_manager" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        set_worker_deployment_manager
                    )
                }
                "set_worker_deployment_ramping_version" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        set_worker_deployment_ramping_version
                    )
                }
                "shutdown_worker" => {
                    rpc_call!(retry_client, call, WorkflowService, shutdown_worker)
                }
                "signal_with_start_workflow_execution" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        signal_with_start_workflow_execution
                    )
                }
                "signal_workflow_execution" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        signal_workflow_execution
                    )
                }
                "start_batch_operation" => {
                    rpc_call!(retry_client, call, WorkflowService, start_batch_operation)
                }
                "start_workflow_execution" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        start_workflow_execution
                    )
                }
                "stop_batch_operation" => {
                    rpc_call!(retry_client, call, WorkflowService, stop_batch_operation)
                }
                "terminate_workflow_execution" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        terminate_workflow_execution
                    )
                }
                "trigger_workflow_rule" => {
                    rpc_call!(retry_client, call, WorkflowService, trigger_workflow_rule)
                }
                "unpause_activity" => {
                    rpc_call!(retry_client, call, WorkflowService, unpause_activity)
                }
                "update_activity_options" => {
                    rpc_call!(retry_client, call, WorkflowService, update_activity_options)
                }
                "update_namespace" => {
                    rpc_call!(retry_client, call, WorkflowService, update_namespace)
                }
                "update_schedule" => {
                    rpc_call!(retry_client, call, WorkflowService, update_schedule)
                }
                "update_task_queue_config" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        update_task_queue_config
                    )
                }
                "update_worker_build_id_compatibility" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        update_worker_build_id_compatibility
                    )
                }
                "update_worker_config" => {
                    rpc_call!(retry_client, call, WorkflowService, update_worker_config)
                }
                "update_worker_deployment_version_metadata" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        update_worker_deployment_version_metadata
                    )
                }
                "update_worker_versioning_rules" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        update_worker_versioning_rules
                    )
                }
                "update_workflow_execution" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        update_workflow_execution
                    )
                }
                "update_workflow_execution_options" => {
                    rpc_call!(
                        retry_client,
                        call,
                        WorkflowService,
                        update_workflow_execution_options
                    )
                }
                _ => {
                    return Err(PyValueError::new_err(format!(
                        "Unknown RPC call {}",
                        call.rpc
                    )))
                }
            }?;
            Ok(bytes)
        })
    }

    fn call_operator_service<'p>(
        &self,
        py: Python<'p>,
        call: RpcCall,
    ) -> PyResult<Bound<'p, PyAny>> {
        self.runtime.assert_same_process("use client")?;
        use temporalio_client::OperatorService;
        let mut retry_client = self.retry_client.clone();
        self.runtime.future_into_py(py, async move {
            let bytes = match call.rpc.as_str() {
                "add_or_update_remote_cluster" => {
                    rpc_call!(
                        retry_client,
                        call,
                        OperatorService,
                        add_or_update_remote_cluster
                    )
                }
                "add_search_attributes" => {
                    rpc_call!(retry_client, call, OperatorService, add_search_attributes)
                }
                "create_nexus_endpoint" => {
                    rpc_call!(retry_client, call, OperatorService, create_nexus_endpoint)
                }
                "delete_namespace" => {
                    rpc_call!(retry_client, call, OperatorService, delete_namespace)
                }
                "delete_nexus_endpoint" => {
                    rpc_call!(retry_client, call, OperatorService, delete_nexus_endpoint)
                }
                "get_nexus_endpoint" => {
                    rpc_call!(retry_client, call, OperatorService, get_nexus_endpoint)
                }
                "list_clusters" => {
                    rpc_call!(retry_client, call, OperatorService, list_clusters)
                }
                "list_nexus_endpoints" => {
                    rpc_call!(retry_client, call, OperatorService, list_nexus_endpoints)
                }
                "list_search_attributes" => {
                    rpc_call!(retry_client, call, OperatorService, list_search_attributes)
                }
                "remove_remote_cluster" => {
                    rpc_call!(retry_client, call, OperatorService, remove_remote_cluster)
                }
                "remove_search_attributes" => {
                    rpc_call!(
                        retry_client,
                        call,
                        OperatorService,
                        remove_search_attributes
                    )
                }
                "update_nexus_endpoint" => {
                    rpc_call!(retry_client, call, OperatorService, update_nexus_endpoint)
                }
                _ => {
                    return Err(PyValueError::new_err(format!(
                        "Unknown RPC call {}",
                        call.rpc
                    )))
                }
            }?;
            Ok(bytes)
        })
    }

    fn call_cloud_service<'p>(&self, py: Python<'p>, call: RpcCall) -> PyResult<Bound<'p, PyAny>> {
        self.runtime.assert_same_process("use client")?;
        use temporalio_client::CloudService;
        let mut retry_client = self.retry_client.clone();
        self.runtime.future_into_py(py, async move {
            let bytes = match call.rpc.as_str() {
                "add_namespace_region" => {
                    rpc_call!(retry_client, call, CloudService, add_namespace_region)
                }
                "add_user_group_member" => {
                    rpc_call!(retry_client, call, CloudService, add_user_group_member)
                }
                "create_api_key" => {
                    rpc_call!(retry_client, call, CloudService, create_api_key)
                }
                "create_connectivity_rule" => {
                    rpc_call!(retry_client, call, CloudService, create_connectivity_rule)
                }
                "create_namespace" => {
                    rpc_call!(retry_client, call, CloudService, create_namespace)
                }
                "create_namespace_export_sink" => {
                    rpc_call!(
                        retry_client,
                        call,
                        CloudService,
                        create_namespace_export_sink
                    )
                }
                "create_nexus_endpoint" => {
                    rpc_call!(retry_client, call, CloudService, create_nexus_endpoint)
                }
                "create_service_account" => {
                    rpc_call!(retry_client, call, CloudService, create_service_account)
                }
                "create_user" => {
                    rpc_call!(retry_client, call, CloudService, create_user)
                }
                "create_user_group" => {
                    rpc_call!(retry_client, call, CloudService, create_user_group)
                }
                "delete_api_key" => {
                    rpc_call!(retry_client, call, CloudService, delete_api_key)
                }
                "delete_connectivity_rule" => {
                    rpc_call!(retry_client, call, CloudService, delete_connectivity_rule)
                }
                "delete_namespace" => {
                    rpc_call!(retry_client, call, CloudService, delete_namespace)
                }
                "delete_namespace_export_sink" => {
                    rpc_call!(
                        retry_client,
                        call,
                        CloudService,
                        delete_namespace_export_sink
                    )
                }
                "delete_namespace_region" => {
                    rpc_call!(retry_client, call, CloudService, delete_namespace_region)
                }
                "delete_nexus_endpoint" => {
                    rpc_call!(retry_client, call, CloudService, delete_nexus_endpoint)
                }
                "delete_service_account" => {
                    rpc_call!(retry_client, call, CloudService, delete_service_account)
                }
                "delete_user" => {
                    rpc_call!(retry_client, call, CloudService, delete_user)
                }
                "delete_user_group" => {
                    rpc_call!(retry_client, call, CloudService, delete_user_group)
                }
                "failover_namespace_region" => {
                    rpc_call!(retry_client, call, CloudService, failover_namespace_region)
                }
                "get_account" => {
                    rpc_call!(retry_client, call, CloudService, get_account)
                }
                "get_api_key" => {
                    rpc_call!(retry_client, call, CloudService, get_api_key)
                }
                "get_api_keys" => {
                    rpc_call!(retry_client, call, CloudService, get_api_keys)
                }
                "get_async_operation" => {
                    rpc_call!(retry_client, call, CloudService, get_async_operation)
                }
                "get_connectivity_rule" => {
                    rpc_call!(retry_client, call, CloudService, get_connectivity_rule)
                }
                "get_connectivity_rules" => {
                    rpc_call!(retry_client, call, CloudService, get_connectivity_rules)
                }
                "get_namespace" => {
                    rpc_call!(retry_client, call, CloudService, get_namespace)
                }
                "get_namespace_export_sink" => {
                    rpc_call!(retry_client, call, CloudService, get_namespace_export_sink)
                }
                "get_namespace_export_sinks" => {
                    rpc_call!(retry_client, call, CloudService, get_namespace_export_sinks)
                }
                "get_namespaces" => {
                    rpc_call!(retry_client, call, CloudService, get_namespaces)
                }
                "get_nexus_endpoint" => {
                    rpc_call!(retry_client, call, CloudService, get_nexus_endpoint)
                }
                "get_nexus_endpoints" => {
                    rpc_call!(retry_client, call, CloudService, get_nexus_endpoints)
                }
                "get_region" => {
                    rpc_call!(retry_client, call, CloudService, get_region)
                }
                "get_regions" => {
                    rpc_call!(retry_client, call, CloudService, get_regions)
                }
                "get_service_account" => {
                    rpc_call!(retry_client, call, CloudService, get_service_account)
                }
                "get_service_accounts" => {
                    rpc_call!(retry_client, call, CloudService, get_service_accounts)
                }
                "get_usage" => {
                    rpc_call!(retry_client, call, CloudService, get_usage)
                }
                "get_user" => {
                    rpc_call!(retry_client, call, CloudService, get_user)
                }
                "get_user_group" => {
                    rpc_call!(retry_client, call, CloudService, get_user_group)
                }
                "get_user_group_members" => {
                    rpc_call!(retry_client, call, CloudService, get_user_group_members)
                }
                "get_user_groups" => {
                    rpc_call!(retry_client, call, CloudService, get_user_groups)
                }
                "get_users" => {
                    rpc_call!(retry_client, call, CloudService, get_users)
                }
                "remove_user_group_member" => {
                    rpc_call!(retry_client, call, CloudService, remove_user_group_member)
                }
                "rename_custom_search_attribute" => {
                    rpc_call!(
                        retry_client,
                        call,
                        CloudService,
                        rename_custom_search_attribute
                    )
                }
                "set_service_account_namespace_access" => {
                    rpc_call!(
                        retry_client,
                        call,
                        CloudService,
                        set_service_account_namespace_access
                    )
                }
                "set_user_group_namespace_access" => {
                    rpc_call!(
                        retry_client,
                        call,
                        CloudService,
                        set_user_group_namespace_access
                    )
                }
                "set_user_namespace_access" => {
                    rpc_call!(retry_client, call, CloudService, set_user_namespace_access)
                }
                "update_account" => {
                    rpc_call!(retry_client, call, CloudService, update_account)
                }
                "update_api_key" => {
                    rpc_call!(retry_client, call, CloudService, update_api_key)
                }
                "update_namespace" => {
                    rpc_call!(retry_client, call, CloudService, update_namespace)
                }
                "update_namespace_export_sink" => {
                    rpc_call!(
                        retry_client,
                        call,
                        CloudService,
                        update_namespace_export_sink
                    )
                }
                "update_namespace_tags" => {
                    rpc_call!(retry_client, call, CloudService, update_namespace_tags)
                }
                "update_nexus_endpoint" => {
                    rpc_call!(retry_client, call, CloudService, update_nexus_endpoint)
                }
                "update_service_account" => {
                    rpc_call!(retry_client, call, CloudService, update_service_account)
                }
                "update_user" => {
                    rpc_call!(retry_client, call, CloudService, update_user)
                }
                "update_user_group" => {
                    rpc_call!(retry_client, call, CloudService, update_user_group)
                }
                "validate_account_audit_log_sink" => {
                    rpc_call!(
                        retry_client,
                        call,
                        CloudService,
                        validate_account_audit_log_sink
                    )
                }
                "validate_namespace_export_sink" => {
                    rpc_call!(
                        retry_client,
                        call,
                        CloudService,
                        validate_namespace_export_sink
                    )
                }
                _ => {
                    return Err(PyValueError::new_err(format!(
                        "Unknown RPC call {}",
                        call.rpc
                    )))
                }
            }?;
            Ok(bytes)
        })
    }

    fn call_test_service<'p>(&self, py: Python<'p>, call: RpcCall) -> PyResult<Bound<'p, PyAny>> {
        self.runtime.assert_same_process("use client")?;
        use temporalio_client::TestService;
        let mut retry_client = self.retry_client.clone();
        self.runtime.future_into_py(py, async move {
            let bytes = match call.rpc.as_str() {
                "get_current_time" => {
                    rpc_call!(retry_client, call, TestService, get_current_time)
                }
                "lock_time_skipping" => {
                    rpc_call!(retry_client, call, TestService, lock_time_skipping)
                }
                "sleep" => {
                    rpc_call!(retry_client, call, TestService, sleep)
                }
                "sleep_until" => {
                    rpc_call!(retry_client, call, TestService, sleep_until)
                }
                "unlock_time_skipping" => {
                    rpc_call!(retry_client, call, TestService, unlock_time_skipping)
                }
                "unlock_time_skipping_with_sleep" => {
                    rpc_call!(
                        retry_client,
                        call,
                        TestService,
                        unlock_time_skipping_with_sleep
                    )
                }
                _ => {
                    return Err(PyValueError::new_err(format!(
                        "Unknown RPC call {}",
                        call.rpc
                    )))
                }
            }?;
            Ok(bytes)
        })
    }

    fn call_health_service<'p>(&self, py: Python<'p>, call: RpcCall) -> PyResult<Bound<'p, PyAny>> {
        self.runtime.assert_same_process("use client")?;
        use temporalio_client::HealthService;
        let mut retry_client = self.retry_client.clone();
        self.runtime.future_into_py(py, async move {
            let bytes = match call.rpc.as_str() {
                "check" => {
                    rpc_call!(retry_client, call, HealthService, check)
                }
                _ => {
                    return Err(PyValueError::new_err(format!(
                        "Unknown RPC call {}",
                        call.rpc
                    )))
                }
            }?;
            Ok(bytes)
        })
    }
}
