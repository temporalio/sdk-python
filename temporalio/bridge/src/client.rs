use pyo3::exceptions::{PyException, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use std::collections::HashMap;
use std::str::FromStr;
use std::time::Duration;
use temporalio_client::tonic::{
    self,
    metadata::{AsciiMetadataKey, AsciiMetadataValue, BinaryMetadataKey, BinaryMetadataValue},
};
use temporalio_client::{
    ClientKeepAliveConfig as CoreClientKeepAliveConfig, ClientOptions, ClientOptionsBuilder,
    ConfiguredClient, HttpConnectProxyOptions, RetryClient, RetryConfig, TemporalServiceClient,
    TlsConfig,
};
use url::Url;

use crate::runtime;

pyo3::create_exception!(temporal_sdk_bridge, RPCError, PyException);

type Client = RetryClient<ConfiguredClient<TemporalServiceClient>>;

#[pyclass]
pub struct ClientRef {
    pub(crate) retry_client: Client,
    pub(crate) runtime: runtime::Runtime,
}

#[derive(FromPyObject)]
pub struct ClientConfig {
    target_url: String,
    client_name: String,
    client_version: String,
    metadata: HashMap<String, RpcMetadataValue>,
    api_key: Option<String>,
    identity: String,
    tls_config: Option<ClientTlsConfig>,
    retry_config: Option<ClientRetryConfig>,
    keep_alive_config: Option<ClientKeepAliveConfig>,
    http_connect_proxy_config: Option<ClientHttpConnectProxyConfig>,
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
struct ClientKeepAliveConfig {
    pub interval_millis: u64,
    pub timeout_millis: u64,
}

#[derive(FromPyObject)]
struct ClientHttpConnectProxyConfig {
    pub target_host: String,
    pub basic_auth: Option<(String, String)>,
}

#[derive(FromPyObject)]
pub(crate) struct RpcCall {
    pub(crate) rpc: String,
    req: Vec<u8>,
    pub(crate) retry: bool,
    metadata: HashMap<String, RpcMetadataValue>,
    timeout_millis: Option<u64>,
}

#[derive(FromPyObject)]
enum RpcMetadataValue {
    #[pyo3(transparent, annotation = "str")]
    Str(String),
    #[pyo3(transparent, annotation = "bytes")]
    Bytes(Vec<u8>),
}

pub fn connect_client<'a>(
    py: Python<'a>,
    runtime_ref: &runtime::RuntimeRef,
    config: ClientConfig,
) -> PyResult<Bound<'a, PyAny>> {
    let opts: ClientOptions = config.try_into()?;
    runtime_ref.runtime.assert_same_process("create client")?;
    let runtime = runtime_ref.runtime.clone();
    runtime_ref.runtime.future_into_py(py, async move {
        Ok(ClientRef {
            retry_client: opts
                .connect_no_namespace(runtime.core.telemetry().get_temporal_metric_meter())
                .await
                .map_err(|err| PyRuntimeError::new_err(format!("Failed client connect: {err}")))?,
            runtime,
        })
    })
}

#[macro_export]
macro_rules! rpc_call {
    ($retry_client:ident, $call:ident, $trait:tt, $call_name:ident) => {
        if $call.retry {
            rpc_resp($trait::$call_name(&mut $retry_client, rpc_req($call)?).await)
        } else {
            rpc_resp($trait::$call_name(&mut $retry_client.into_inner(), rpc_req($call)?).await)
        }
    };
}

#[pymethods]
impl ClientRef {
    fn update_metadata(&self, headers: HashMap<String, RpcMetadataValue>) -> PyResult<()> {
        let (ascii_headers, binary_headers) = partition_headers(headers);

        self.retry_client
            .get_client()
            .set_headers(ascii_headers)
            .map_err(|err| PyValueError::new_err(err.to_string()))?;
        self.retry_client
            .get_client()
            .set_binary_headers(binary_headers)
            .map_err(|err| PyValueError::new_err(err.to_string()))?;

        Ok(())
    }

    fn update_api_key(&self, api_key: Option<String>) {
        self.retry_client.get_client().set_api_key(api_key);
    }
}

pub(crate) fn rpc_req<P: prost::Message + Default>(call: RpcCall) -> PyResult<tonic::Request<P>> {
    let proto = P::decode(&*call.req)
        .map_err(|err| PyValueError::new_err(format!("Invalid proto: {err}")))?;
    let mut req = tonic::Request::new(proto);
    for (k, v) in call.metadata {
        if let Ok(binary_key) = BinaryMetadataKey::from_str(&k) {
            let RpcMetadataValue::Bytes(bytes) = v else {
                return Err(PyValueError::new_err(format!(
                    "Invalid metadata value for binary key {k}: expected bytes"
                )));
            };

            req.metadata_mut()
                .insert_bin(binary_key, BinaryMetadataValue::from_bytes(&bytes));
        } else {
            let ascii_key = AsciiMetadataKey::from_str(&k)
                .map_err(|err| PyValueError::new_err(format!("Invalid metadata key: {err}")))?;

            let RpcMetadataValue::Str(string) = v else {
                return Err(PyValueError::new_err(format!(
                    "Invalid metadata value for ASCII key {k}: expected str"
                )));
            };

            req.metadata_mut().insert(
                ascii_key,
                AsciiMetadataValue::from_str(&string).map_err(|err| {
                    PyValueError::new_err(format!("Invalid metadata value: {err}"))
                })?,
            );
        }
    }
    if let Some(timeout_millis) = call.timeout_millis {
        req.set_timeout(Duration::from_millis(timeout_millis));
    }
    Ok(req)
}

pub(crate) fn rpc_resp<P>(res: Result<tonic::Response<P>, tonic::Status>) -> PyResult<Vec<u8>>
where
    P: prost::Message,
    P: Default,
{
    match res {
        Ok(resp) => Ok(resp.get_ref().encode_to_vec()),
        Err(err) => {
            Python::with_gil(move |py| {
                // Create tuple of "status", "message", and optional "details"
                let code = err.code() as u32;
                let message = err.message().to_owned();
                let details = err.details().into_pyobject(py)?.unbind();
                Err(RPCError::new_err((code, message, details)))
            })
        }
    }
}

fn partition_headers(
    headers: HashMap<String, RpcMetadataValue>,
) -> (HashMap<String, String>, HashMap<String, Vec<u8>>) {
    let (ascii_enum_headers, binary_enum_headers): (HashMap<_, _>, HashMap<_, _>) = headers
        .into_iter()
        .partition(|(_, v)| matches!(v, RpcMetadataValue::Str(_)));

    let ascii_headers = ascii_enum_headers
        .into_iter()
        .map(|(k, v)| {
            let RpcMetadataValue::Str(s) = v else {
                unreachable!();
            };
            (k, s)
        })
        .collect();
    let binary_headers = binary_enum_headers
        .into_iter()
        .map(|(k, v)| {
            let RpcMetadataValue::Bytes(b) = v else {
                unreachable!();
            };
            (k, b)
        })
        .collect();

    (ascii_headers, binary_headers)
}

impl TryFrom<ClientConfig> for ClientOptions {
    type Error = PyErr;

    fn try_from(opts: ClientConfig) -> PyResult<Self> {
        let mut gateway_opts = ClientOptionsBuilder::default();
        let (ascii_headers, binary_headers) = partition_headers(opts.metadata);
        gateway_opts
            .target_url(
                Url::parse(&opts.target_url)
                    .map_err(|err| PyValueError::new_err(format!("invalid target URL: {err}")))?,
            )
            .client_name(opts.client_name)
            .client_version(opts.client_version)
            .identity(opts.identity)
            .retry_config(
                opts.retry_config
                    .map_or(RetryConfig::default(), |c| c.into()),
            )
            .keep_alive(opts.keep_alive_config.map(Into::into))
            .http_connect_proxy(opts.http_connect_proxy_config.map(Into::into))
            .headers(Some(ascii_headers))
            .binary_headers(Some(binary_headers))
            .api_key(opts.api_key);
        // Builder does not allow us to set option here, so we have to make
        // a conditional to even call it
        if let Some(tls_config) = opts.tls_config {
            gateway_opts.tls_cfg(tls_config.try_into()?);
        }
        gateway_opts
            .build()
            .map_err(|err| PyValueError::new_err(format!("Invalid client config: {err}")))
    }
}

impl TryFrom<ClientTlsConfig> for temporalio_client::TlsConfig {
    type Error = PyErr;

    fn try_from(conf: ClientTlsConfig) -> PyResult<Self> {
        Ok(TlsConfig {
            server_root_ca_cert: conf.server_root_ca_cert,
            domain: conf.domain,
            client_tls_config: match (conf.client_cert, conf.client_private_key) {
                (None, None) => None,
                (Some(client_cert), Some(client_private_key)) => {
                    Some(temporalio_client::ClientTlsConfig {
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

impl From<ClientKeepAliveConfig> for CoreClientKeepAliveConfig {
    fn from(conf: ClientKeepAliveConfig) -> Self {
        CoreClientKeepAliveConfig {
            interval: Duration::from_millis(conf.interval_millis),
            timeout: Duration::from_millis(conf.timeout_millis),
        }
    }
}

impl From<ClientHttpConnectProxyConfig> for HttpConnectProxyOptions {
    fn from(conf: ClientHttpConnectProxyConfig) -> Self {
        HttpConnectProxyOptions {
            target_addr: conf.target_host,
            basic_auth: conf.basic_auth,
        }
    }
}
