use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use std::collections::HashMap;
use std::time::Duration;
use temporal_client::WorkflowService;
use tonic;

#[pymodule]
fn temporal_sdk_bridge(py: Python, m: &PyModule) -> PyResult<()> {
    m.add("RPCError", py.get_type::<RPCError>())?;
    m.add_class::<ClientRef>()?;
    m.add_function(wrap_pyfunction!(connect_client, m)?)?;
    Ok(())
}

pyo3::create_exception!(temporal_sdk_bridge, RPCError, pyo3::exceptions::PyException);

type Client = temporal_client::RetryGateway<
    temporal_client::ConfiguredClient<temporal_client::WorkflowServiceClientWithMetrics>,
>;

#[pyclass]
pub struct ClientRef {
    retry_client: Client,
}

#[derive(FromPyObject)]
pub struct ClientOptions {
    target_url: String,
    client_name: String,
    client_version: String,
    static_headers: HashMap<String, String>,
    identity: String,
    worker_binary_id: String,
    tls_config: Option<ClientTlsConfig>,
    retry_config: Option<ClientRetryConfig>,
}

#[derive(FromPyObject)]
pub struct ClientTlsConfig {
    server_root_ca_cert: Option<Vec<u8>>,
    domain: Option<String>,
    client_cert: Option<Vec<u8>>,
    client_private_key: Option<Vec<u8>>,
}

#[derive(FromPyObject)]
pub struct ClientRetryConfig {
    pub initial_interval_millis: u64,
    pub randomization_factor: f64,
    pub multiplier: f64,
    pub max_interval_millis: u64,
    pub max_elapsed_time_millis: Option<u64>,
    pub max_retries: usize,
}

#[pyfunction]
fn connect_client(py: Python, opts: ClientOptions) -> PyResult<&PyAny> {
    // TODO(cretz): Add metrics_meter?
    let opts: temporal_client::ServerGatewayOptions = opts.try_into()?;
    pyo3_asyncio::tokio::future_into_py(py, async move {
        Ok(ClientRef {
            retry_client: opts.connect_no_namespace(None).await.map_err(|err| {
                PyRuntimeError::new_err(format!("Failed client connect: {}", err))
            })?,
        })
    })
}

#[pymethods]
impl ClientRef {
    fn call<'p>(
        &self,
        py: Python<'p>,
        rpc: String,
        retry: bool,
        req: Vec<u8>,
    ) -> PyResult<&'p PyAny> {
        let mut retry_client = self.retry_client.clone();
        pyo3_asyncio::tokio::future_into_py(py, async move {
            let bytes = match rpc.as_str() {
                "count_workflow_executions" => {
                    rpc_call!(retry_client, retry, count_workflow_executions, req)
                }
                "deprecate_namespace" => rpc_call!(retry_client, retry, deprecate_namespace, req),
                "describe_namespace" => rpc_call!(retry_client, retry, describe_namespace, req),
                "describe_task_queue" => rpc_call!(retry_client, retry, describe_task_queue, req),
                "describe_workflow_execution" => {
                    rpc_call!(retry_client, retry, describe_workflow_execution, req)
                }
                "get_cluster_info" => rpc_call!(retry_client, retry, get_cluster_info, req),
                "get_search_attributes" => {
                    rpc_call!(retry_client, retry, get_search_attributes, req)
                }
                "get_workflow_execution_history" => {
                    rpc_call!(retry_client, retry, get_workflow_execution_history, req)
                }
                "list_archived_workflow_executions" => {
                    rpc_call!(retry_client, retry, list_archived_workflow_executions, req)
                }
                "list_closed_workflow_executions" => {
                    rpc_call!(retry_client, retry, list_closed_workflow_executions, req)
                }
                "list_namespaces" => rpc_call!(retry_client, retry, list_namespaces, req),
                "list_open_workflow_executions" => {
                    rpc_call!(retry_client, retry, list_open_workflow_executions, req)
                }
                "list_task_queue_partitions" => {
                    rpc_call!(retry_client, retry, list_task_queue_partitions, req)
                }
                "list_workflow_executions" => {
                    rpc_call!(retry_client, retry, list_workflow_executions, req)
                }
                "poll_activity_task_queue" => {
                    rpc_call!(retry_client, retry, poll_activity_task_queue, req)
                }
                "poll_workflow_task_queue" => {
                    rpc_call!(retry_client, retry, poll_workflow_task_queue, req)
                }
                "query_workflow" => rpc_call!(retry_client, retry, query_workflow, req),
                "record_activity_task_heartbeat" => {
                    rpc_call!(retry_client, retry, record_activity_task_heartbeat, req)
                }
                "record_activity_task_heartbeat_by_id" => rpc_call!(
                    retry_client,
                    retry,
                    record_activity_task_heartbeat_by_id,
                    req
                ),
                "register_namespace" => rpc_call!(retry_client, retry, register_namespace, req),
                "request_cancel_workflow_execution" => {
                    rpc_call!(retry_client, retry, request_cancel_workflow_execution, req)
                }
                "reset_sticky_task_queue" => {
                    rpc_call!(retry_client, retry, reset_sticky_task_queue, req)
                }
                "reset_workflow_execution" => {
                    rpc_call!(retry_client, retry, reset_workflow_execution, req)
                }
                "respond_activity_task_canceled" => {
                    rpc_call!(retry_client, retry, respond_activity_task_canceled, req)
                }
                "respond_activity_task_canceled_by_id" => rpc_call!(
                    retry_client,
                    retry,
                    respond_activity_task_canceled_by_id,
                    req
                ),
                "respond_activity_task_completed" => {
                    rpc_call!(retry_client, retry, respond_activity_task_completed, req)
                }
                "respond_activity_task_completed_by_id" => rpc_call!(
                    retry_client,
                    retry,
                    respond_activity_task_completed_by_id,
                    req
                ),
                "respond_activity_task_failed" => {
                    rpc_call!(retry_client, retry, respond_activity_task_failed, req)
                }
                "respond_activity_task_failed_by_id" => {
                    rpc_call!(retry_client, retry, respond_activity_task_failed_by_id, req)
                }
                "respond_query_task_completed" => {
                    rpc_call!(retry_client, retry, respond_query_task_completed, req)
                }
                "respond_workflow_task_completed" => {
                    rpc_call!(retry_client, retry, respond_workflow_task_completed, req)
                }
                "respond_workflow_task_failed" => {
                    rpc_call!(retry_client, retry, respond_workflow_task_failed, req)
                }
                "scan_workflow_executions" => {
                    rpc_call!(retry_client, retry, scan_workflow_executions, req)
                }
                "signal_with_start_workflow_execution" => rpc_call!(
                    retry_client,
                    retry,
                    signal_with_start_workflow_execution,
                    req
                ),
                "signal_workflow_execution" => {
                    rpc_call!(retry_client, retry, signal_workflow_execution, req)
                }
                "start_workflow_execution" => {
                    rpc_call!(retry_client, retry, start_workflow_execution, req)
                }
                "terminate_workflow_execution" => {
                    rpc_call!(retry_client, retry, terminate_workflow_execution, req)
                }
                "update_namespace" => rpc_call!(retry_client, retry, update_namespace, req),
                _ => return Err(PyValueError::new_err(format!("Unknown RPC call {}", rpc))),
            }?;
            let bytes: &[u8] = &bytes;
            Ok(Python::with_gil(|py| bytes.into_py(py)))
        })
    }
}

fn rpc_req<P>(bytes: Vec<u8>) -> PyResult<tonic::Request<P>>
where
    P: prost::Message,
    P: Default,
{
    let proto = P::decode(&*bytes)
        .map_err(|err| PyValueError::new_err(format!("Invalid proto: {}", err)))?;
    Ok(tonic::Request::new(proto))
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

#[macro_export]
macro_rules! rpc_call {
    ($retry_client:ident, $retry:ident, $call_name:ident, $req:ident) => {
        if $retry {
            rpc_resp($retry_client.$call_name(rpc_req($req)?).await)
        } else {
            rpc_resp($retry_client.into_inner().$call_name(rpc_req($req)?).await)
        }
    };
}

impl TryFrom<ClientOptions> for temporal_client::ServerGatewayOptions {
    type Error = PyErr;

    fn try_from(opts: ClientOptions) -> PyResult<Self> {
        let mut gateway_opts = temporal_client::ServerGatewayOptionsBuilder::default();
        gateway_opts
            .target_url(
                url::Url::parse(&opts.target_url)
                    .map_err(|err| PyValueError::new_err(format!("invalid target URL: {}", err)))?,
            )
            .client_name(opts.client_name)
            .client_version(opts.client_version)
            .static_headers(opts.static_headers)
            .identity(opts.identity)
            .worker_binary_id(opts.worker_binary_id)
            .retry_config(
                opts.retry_config
                    .map_or(temporal_client::RetryConfig::default(), |c| c.into()),
            );
        // Builder does not allow us to set option here, so we have to make
        // a conditional to even call it
        if let Some(tls_config) = opts.tls_config {
            gateway_opts.tls_cfg(tls_config.try_into()?);
        }
        return gateway_opts
            .build()
            .map_err(|err| PyValueError::new_err(format!("Invalid client options: {}", err)));
    }
}

impl TryFrom<ClientTlsConfig> for temporal_client::TlsConfig {
    type Error = PyErr;

    fn try_from(conf: ClientTlsConfig) -> PyResult<Self> {
        Ok(temporal_client::TlsConfig {
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

impl From<ClientRetryConfig> for temporal_client::RetryConfig {
    fn from(conf: ClientRetryConfig) -> Self {
        temporal_client::RetryConfig {
            initial_interval: Duration::from_millis(conf.initial_interval_millis),
            randomization_factor: conf.randomization_factor,
            multiplier: conf.multiplier,
            max_interval: Duration::from_millis(conf.max_interval_millis),
            max_elapsed_time: conf.max_elapsed_time_millis.map(Duration::from_millis),
            max_retries: conf.max_retries,
        }
    }
}
