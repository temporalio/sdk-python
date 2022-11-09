use parking_lot::RwLock;
use pyo3::exceptions::{PyException, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use std::collections::HashMap;
use std::str::FromStr;
use std::sync::Arc;
use std::time::Duration;
use temporal_client::{
    ClientOptions, ClientOptionsBuilder, ConfiguredClient, HealthService, OperatorService,
    RetryClient, RetryConfig, TemporalServiceClientWithMetrics, TestService, TlsConfig,
    WorkflowService,
};
use tonic::metadata::MetadataKey;
use url::Url;

use crate::runtime;

pyo3::create_exception!(temporal_sdk_bridge, RPCError, PyException);

type Client = RetryClient<ConfiguredClient<TemporalServiceClientWithMetrics>>;

#[pyclass]
pub struct ClientRef {
    pub(crate) retry_client: Client,
    runtime: runtime::Runtime,
}

#[derive(FromPyObject)]
pub struct ClientConfig {
    target_url: String,
    client_name: String,
    client_version: String,
    metadata: HashMap<String, String>,
    identity: String,
    tls_config: Option<ClientTlsConfig>,
    retry_config: Option<ClientRetryConfig>,
}

#[derive(FromPyObject)]
struct ClientTlsConfig {
    server_root_ca_cert: Option<Vec<u8>>,
    domain: Option<String>,
    client_cert: Option<Vec<u8>>,
    client_private_key: Option<Vec<u8>>,
}

#[derive(FromPyObject)]
struct ClientRetryConfig {
    pub initial_interval_millis: u64,
    pub randomization_factor: f64,
    pub multiplier: f64,
    pub max_interval_millis: u64,
    pub max_elapsed_time_millis: Option<u64>,
    pub max_retries: usize,
}

#[derive(FromPyObject)]
struct RpcCall {
    rpc: String,
    req: Vec<u8>,
    retry: bool,
    metadata: HashMap<String, String>,
    timeout_millis: Option<u64>,
}

pub fn connect_client<'a>(
    py: Python<'a>,
    runtime_ref: &runtime::RuntimeRef,
    config: ClientConfig,
) -> PyResult<&'a PyAny> {
    let headers = if config.metadata.is_empty() {
        None
    } else {
        Some(Arc::new(RwLock::new(config.metadata.clone())))
    };
    let opts: ClientOptions = config.try_into()?;
    let runtime = runtime_ref.runtime.clone();
    runtime_ref.runtime.future_into_py(py, async move {
        Ok(ClientRef {
            retry_client: opts
                .connect_no_namespace(runtime.core.metric_meter(), headers)
                .await
                .map_err(|err| {
                    PyRuntimeError::new_err(format!("Failed client connect: {}", err))
                })?,
            runtime,
        })
    })
}

macro_rules! rpc_call {
    ($retry_client:ident, $call:ident, $call_name:ident) => {
        if $call.retry {
            rpc_resp($retry_client.$call_name(rpc_req($call)?).await)
        } else {
            rpc_resp($retry_client.into_inner().$call_name(rpc_req($call)?).await)
        }
    };
}

#[pymethods]
impl ClientRef {
    fn update_metadata(&self, headers: HashMap<String, String>) {
        self.retry_client.get_client().set_headers(headers);
    }

    fn call_workflow_service<'p>(&self, py: Python<'p>, call: RpcCall) -> PyResult<&'p PyAny> {
        let mut retry_client = self.retry_client.clone();
        self.runtime.future_into_py(py, async move {
            let bytes = match call.rpc.as_str() {
                "count_workflow_executions" => {
                    rpc_call!(retry_client, call, count_workflow_executions)
                }
                "create_schedule" => {
                    rpc_call!(retry_client, call, create_schedule)
                }
                "delete_schedule" => {
                    rpc_call!(retry_client, call, delete_schedule)
                }
                "deprecate_namespace" => rpc_call!(retry_client, call, deprecate_namespace),
                "describe_namespace" => rpc_call!(retry_client, call, describe_namespace),
                "describe_schedule" => rpc_call!(retry_client, call, describe_schedule),
                "describe_task_queue" => rpc_call!(retry_client, call, describe_task_queue),
                "describe_workflow_execution" => {
                    rpc_call!(retry_client, call, describe_workflow_execution)
                }
                "get_cluster_info" => rpc_call!(retry_client, call, get_cluster_info),
                "get_search_attributes" => {
                    rpc_call!(retry_client, call, get_search_attributes)
                }
                "get_system_info" => rpc_call!(retry_client, call, get_system_info),
                "get_worker_build_id_ordering" => {
                    rpc_call!(retry_client, call, get_worker_build_id_ordering)
                }
                "get_workflow_execution_history" => {
                    rpc_call!(retry_client, call, get_workflow_execution_history)
                }
                "get_workflow_execution_history_reverse" => {
                    rpc_call!(retry_client, call, get_workflow_execution_history_reverse)
                }
                "list_archived_workflow_executions" => {
                    rpc_call!(retry_client, call, list_archived_workflow_executions)
                }
                "list_closed_workflow_executions" => {
                    rpc_call!(retry_client, call, list_closed_workflow_executions)
                }
                "list_namespaces" => rpc_call!(retry_client, call, list_namespaces),
                "list_open_workflow_executions" => {
                    rpc_call!(retry_client, call, list_open_workflow_executions)
                }
                "list_schedule_matching_times" => {
                    rpc_call!(retry_client, call, list_schedule_matching_times)
                }
                "list_schedules" => {
                    rpc_call!(retry_client, call, list_schedules)
                }
                "list_task_queue_partitions" => {
                    rpc_call!(retry_client, call, list_task_queue_partitions)
                }
                "list_workflow_executions" => {
                    rpc_call!(retry_client, call, list_workflow_executions)
                }
                "patch_schedule" => {
                    rpc_call!(retry_client, call, patch_schedule)
                }
                "poll_activity_task_queue" => {
                    rpc_call!(retry_client, call, poll_activity_task_queue)
                }
                "poll_workflow_task_queue" => {
                    rpc_call!(retry_client, call, poll_workflow_task_queue)
                }
                "query_workflow" => rpc_call!(retry_client, call, query_workflow),
                "record_activity_task_heartbeat" => {
                    rpc_call!(retry_client, call, record_activity_task_heartbeat)
                }
                "record_activity_task_heartbeat_by_id" => {
                    rpc_call!(retry_client, call, record_activity_task_heartbeat_by_id)
                }
                "register_namespace" => rpc_call!(retry_client, call, register_namespace),
                "request_cancel_workflow_execution" => {
                    rpc_call!(retry_client, call, request_cancel_workflow_execution)
                }
                "reset_sticky_task_queue" => {
                    rpc_call!(retry_client, call, reset_sticky_task_queue)
                }
                "reset_workflow_execution" => {
                    rpc_call!(retry_client, call, reset_workflow_execution)
                }
                "respond_activity_task_canceled" => {
                    rpc_call!(retry_client, call, respond_activity_task_canceled)
                }
                "respond_activity_task_canceled_by_id" => {
                    rpc_call!(retry_client, call, respond_activity_task_canceled_by_id)
                }
                "respond_activity_task_completed" => {
                    rpc_call!(retry_client, call, respond_activity_task_completed)
                }
                "respond_activity_task_completed_by_id" => {
                    rpc_call!(retry_client, call, respond_activity_task_completed_by_id)
                }
                "respond_activity_task_failed" => {
                    rpc_call!(retry_client, call, respond_activity_task_failed)
                }
                "respond_activity_task_failed_by_id" => {
                    rpc_call!(retry_client, call, respond_activity_task_failed_by_id)
                }
                "respond_query_task_completed" => {
                    rpc_call!(retry_client, call, respond_query_task_completed)
                }
                "respond_workflow_task_completed" => {
                    rpc_call!(retry_client, call, respond_workflow_task_completed)
                }
                "respond_workflow_task_failed" => {
                    rpc_call!(retry_client, call, respond_workflow_task_failed)
                }
                "scan_workflow_executions" => {
                    rpc_call!(retry_client, call, scan_workflow_executions)
                }
                "signal_with_start_workflow_execution" => {
                    rpc_call!(retry_client, call, signal_with_start_workflow_execution)
                }
                "signal_workflow_execution" => {
                    rpc_call!(retry_client, call, signal_workflow_execution)
                }
                "start_workflow_execution" => {
                    rpc_call!(retry_client, call, start_workflow_execution)
                }
                "terminate_workflow_execution" => {
                    rpc_call!(retry_client, call, terminate_workflow_execution)
                }
                "update_namespace" => rpc_call!(retry_client, call, update_namespace),
                "update_schedule" => rpc_call!(retry_client, call, update_schedule),
                "update_workflow" => rpc_call!(retry_client, call, update_workflow),
                "update_worker_build_id_ordering" => {
                    rpc_call!(retry_client, call, update_worker_build_id_ordering)
                }
                _ => {
                    return Err(PyValueError::new_err(format!(
                        "Unknown RPC call {}",
                        call.rpc
                    )))
                }
            }?;
            let bytes: &[u8] = &bytes;
            Ok(Python::with_gil(|py| bytes.into_py(py)))
        })
    }

    fn call_operator_service<'p>(&self, py: Python<'p>, call: RpcCall) -> PyResult<&'p PyAny> {
        let mut retry_client = self.retry_client.clone();
        self.runtime.future_into_py(py, async move {
            let bytes = match call.rpc.as_str() {
                "add_or_update_remote_cluster" => {
                    rpc_call!(retry_client, call, add_or_update_remote_cluster)
                }
                "add_search_attributes" => {
                    rpc_call!(retry_client, call, add_search_attributes)
                }
                "delete_namespace" => rpc_call!(retry_client, call, delete_namespace),
                "delete_workflow_execution" => {
                    rpc_call!(retry_client, call, delete_workflow_execution)
                }
                "list_clusters" => rpc_call!(retry_client, call, list_clusters),
                "list_search_attributes" => {
                    rpc_call!(retry_client, call, list_search_attributes)
                }
                "remove_remote_cluster" => {
                    rpc_call!(retry_client, call, remove_remote_cluster)
                }
                "remove_search_attributes" => {
                    rpc_call!(retry_client, call, remove_search_attributes)
                }
                _ => {
                    return Err(PyValueError::new_err(format!(
                        "Unknown RPC call {}",
                        call.rpc
                    )))
                }
            }?;
            let bytes: &[u8] = &bytes;
            Ok(Python::with_gil(|py| bytes.into_py(py)))
        })
    }

    fn call_test_service<'p>(&self, py: Python<'p>, call: RpcCall) -> PyResult<&'p PyAny> {
        let mut retry_client = self.retry_client.clone();
        self.runtime.future_into_py(py, async move {
            let bytes = match call.rpc.as_str() {
                "get_current_time" => rpc_call!(retry_client, call, get_current_time),
                "lock_time_skipping" => rpc_call!(retry_client, call, lock_time_skipping),
                "sleep_until" => rpc_call!(retry_client, call, sleep_until),
                "sleep" => rpc_call!(retry_client, call, sleep),
                "unlock_time_skipping_with_sleep" => {
                    rpc_call!(retry_client, call, unlock_time_skipping_with_sleep)
                }
                "unlock_time_skipping" => rpc_call!(retry_client, call, unlock_time_skipping),
                _ => {
                    return Err(PyValueError::new_err(format!(
                        "Unknown RPC call {}",
                        call.rpc
                    )))
                }
            }?;
            let bytes: &[u8] = &bytes;
            Ok(Python::with_gil(|py| bytes.into_py(py)))
        })
    }

    fn call_health_service<'p>(&self, py: Python<'p>, call: RpcCall) -> PyResult<&'p PyAny> {
        let mut retry_client = self.retry_client.clone();
        self.runtime.future_into_py(py, async move {
            let bytes = match call.rpc.as_str() {
                "check" => rpc_call!(retry_client, call, check),
                _ => {
                    return Err(PyValueError::new_err(format!(
                        "Unknown RPC call {}",
                        call.rpc
                    )))
                }
            }?;
            let bytes: &[u8] = &bytes;
            Ok(Python::with_gil(|py| bytes.into_py(py)))
        })
    }
}

fn rpc_req<P: prost::Message + Default>(call: RpcCall) -> PyResult<tonic::Request<P>> {
    let proto = P::decode(&*call.req)
        .map_err(|err| PyValueError::new_err(format!("Invalid proto: {}", err)))?;
    let mut req = tonic::Request::new(proto);
    for (k, v) in call.metadata {
        req.metadata_mut().insert(
            MetadataKey::from_str(k.as_str())
                .map_err(|err| PyValueError::new_err(format!("Invalid metadata key: {}", err)))?,
            v.parse()
                .map_err(|err| PyValueError::new_err(format!("Invalid metadata value: {}", err)))?,
        );
    }
    if let Some(timeout_millis) = call.timeout_millis {
        req.set_timeout(Duration::from_millis(timeout_millis));
    }
    Ok(req)
}

fn rpc_resp<P>(res: Result<tonic::Response<P>, tonic::Status>) -> PyResult<Vec<u8>>
where
    P: prost::Message,
    P: Default,
{
    match res {
        Ok(resp) => Ok(resp.get_ref().encode_to_vec()),
        Err(err) => {
            Err(Python::with_gil(move |py| {
                // Create tuple of "status", "message", and optional "details"
                let code = err.code() as u32;
                let message = err.message().to_owned();
                let details = err.details().into_py(py);
                RPCError::new_err((code, message, details))
            }))
        }
    }
}

impl TryFrom<ClientConfig> for ClientOptions {
    type Error = PyErr;

    fn try_from(opts: ClientConfig) -> PyResult<Self> {
        let mut gateway_opts = ClientOptionsBuilder::default();
        gateway_opts
            .target_url(
                Url::parse(&opts.target_url)
                    .map_err(|err| PyValueError::new_err(format!("invalid target URL: {}", err)))?,
            )
            .client_name(opts.client_name)
            .client_version(opts.client_version)
            .identity(opts.identity)
            .retry_config(
                opts.retry_config
                    .map_or(RetryConfig::default(), |c| c.into()),
            );
        // Builder does not allow us to set option here, so we have to make
        // a conditional to even call it
        if let Some(tls_config) = opts.tls_config {
            gateway_opts.tls_cfg(tls_config.try_into()?);
        }
        return gateway_opts
            .build()
            .map_err(|err| PyValueError::new_err(format!("Invalid client config: {}", err)));
    }
}

impl TryFrom<ClientTlsConfig> for temporal_client::TlsConfig {
    type Error = PyErr;

    fn try_from(conf: ClientTlsConfig) -> PyResult<Self> {
        Ok(TlsConfig {
            server_root_ca_cert: conf.server_root_ca_cert,
            domain: conf.domain,
            client_tls_config: match (conf.client_cert, conf.client_private_key) {
                (None, None) => None,
                (Some(client_cert), Some(client_private_key)) => {
                    Some(temporal_client::ClientTlsConfig {
                        client_cert,
                        client_private_key,
                    })
                }
                _ => {
                    return Err(PyValueError::new_err(
                        "Must have both client cert and private key or neither",
                    ))
                }
            },
        })
    }
}

impl From<ClientRetryConfig> for RetryConfig {
    fn from(conf: ClientRetryConfig) -> Self {
        RetryConfig {
            initial_interval: Duration::from_millis(conf.initial_interval_millis),
            randomization_factor: conf.randomization_factor,
            multiplier: conf.multiplier,
            max_interval: Duration::from_millis(conf.max_interval_millis),
            max_elapsed_time: conf.max_elapsed_time_millis.map(Duration::from_millis),
            max_retries: conf.max_retries,
        }
    }
}
