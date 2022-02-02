use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use std::collections::HashMap;
use std::time::Duration;
use tonic;

#[pymodule]
fn temporal_sdk_bridge(_py: Python, m: &PyModule) -> PyResult<()> {
    m.add_class::<ClientRef>()?;
    m.add_function(wrap_pyfunction!(new_client, m)?)?;
    Ok(())
}

#[pyclass]
pub struct ClientRef {
    retry_client: std::sync::Arc<temporal_client::RetryGateway<temporal_client::ServerGateway>>,
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
fn new_client(py: Python, opts: ClientOptions) -> PyResult<&PyAny> {
    // TODO(cretz): Add metrics_meter?
    let opts: temporal_client::ServerGatewayOptions = opts.try_into()?;
    pyo3_asyncio::tokio::future_into_py(py, async move {
        Ok(ClientRef {
            retry_client: std::sync::Arc::new(opts.connect(None).await.map_err(|err| {
                PyRuntimeError::new_err(format!("Failed client connect: {}", err))
            })?),
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
        let retry_client = self.retry_client.clone();
        pyo3_asyncio::tokio::future_into_py(py, async move {
            let bytes = match rpc.as_str() {
                "get_workflow_execution_history" => {
                    rpc_call!(retry_client, retry, get_workflow_execution_history, req)
                }
                "start_workflow_execution" => {
                    rpc_call!(retry_client, retry, start_workflow_execution, req)
                }
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
        // TODO(cretz): Better error struct here w/ all the details
        Err(err) => Err(PyRuntimeError::new_err(format!("RPC failed: {}", err))),
    }
}

fn clone_tonic_req<T: Clone>(req: &tonic::Request<T>) -> tonic::Request<T> {
    tonic::Request::new(req.get_ref().clone())
}

#[macro_export]
macro_rules! rpc_call {
    ($retry_client:ident, $retry:ident, $call_name:ident, $req:ident) => {
        if $retry {
            // TODO(cretz): I wouldn't have to clone this if call_with_retry
            // allowed error types other than tonic statuses
            let req = rpc_req($req)?;
            let fact = || {
                let req = clone_tonic_req(&req);
                let mut raw_client = $retry_client.get_client().raw_client().clone();
                async move { raw_client.$call_name(req).await }
            };
            rpc_resp(
                $retry_client
                    .call_with_retry(
                        fact,
                        temporal_client::CallType::Normal,
                        stringify!($call_name),
                    )
                    .await,
            )
        } else {
            let mut raw_client = $retry_client.get_client().raw_client().clone();
            rpc_resp(raw_client.$call_name(rpc_req($req)?).await)
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
            // TODO(cretz): Unneeded
            .namespace("".to_string())
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
