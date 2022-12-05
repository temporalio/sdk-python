use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::AsPyPointer;
use std::collections::HashMap;
use std::future::Future;
use std::net::SocketAddr;
use std::pin::Pin;
use std::str::FromStr;
use std::sync::Arc;
use std::time::Duration;
use temporal_sdk_core::CoreRuntime;
use temporal_sdk_core_api::telemetry::{
    Logger, MetricsExporter, OtelCollectorOptions, TelemetryOptions, TelemetryOptionsBuilder,
    TraceExportConfig, TraceExporter,
};
use url::Url;

#[pyclass]
pub struct RuntimeRef {
    pub(crate) runtime: Runtime,
}

#[derive(Clone)]
pub(crate) struct Runtime {
    pub(crate) core: Arc<CoreRuntime>,
}

#[derive(FromPyObject)]
pub struct TelemetryConfig {
    tracing: Option<TracingConfig>,
    logging: Option<LoggingConfig>,
    metrics: Option<MetricsConfig>,
}

#[derive(FromPyObject)]
pub struct TracingConfig {
    filter: String,
    opentelemetry: OpenTelemetryConfig,
}

#[derive(FromPyObject)]
pub struct LoggingConfig {
    filter: String,
    forward: bool,
}

#[derive(FromPyObject)]
pub struct MetricsConfig {
    opentelemetry: Option<OpenTelemetryConfig>,
    prometheus: Option<PrometheusConfig>,
}

#[derive(FromPyObject)]
pub struct OpenTelemetryConfig {
    url: String,
    headers: HashMap<String, String>,
    metric_periodicity_millis: Option<u64>,
}

#[derive(FromPyObject)]
pub struct PrometheusConfig {
    bind_address: String,
}

pub fn init_runtime(telemetry_config: TelemetryConfig) -> PyResult<RuntimeRef> {
    Ok(RuntimeRef {
        runtime: Runtime {
            core: Arc::new(
                CoreRuntime::new(
                    telemetry_config.try_into()?,
                    tokio::runtime::Builder::new_multi_thread(),
                )
                .map_err(|err| {
                    PyRuntimeError::new_err(format!("Failed initializing telemetry: {}", err))
                })?,
            ),
        },
    })
}

pub fn raise_in_thread<'a>(_py: Python<'a>, thread_id: std::os::raw::c_long, exc: &PyAny) -> bool {
    unsafe { pyo3::ffi::PyThreadState_SetAsyncExc(thread_id, exc.as_ptr()) == 1 }
}

impl Runtime {
    pub fn future_into_py<'a, F, T>(&self, py: Python<'a>, fut: F) -> PyResult<&'a PyAny>
    where
        F: Future<Output = PyResult<T>> + Send + 'static,
        T: IntoPy<PyObject>,
    {
        let _guard = self.core.tokio_handle().enter();
        pyo3_asyncio::generic::future_into_py::<TokioRuntime, _, T>(py, fut)
    }
}

impl TryFrom<TelemetryConfig> for TelemetryOptions {
    type Error = PyErr;

    fn try_from(conf: TelemetryConfig) -> PyResult<Self> {
        let mut build = TelemetryOptionsBuilder::default();
        if let Some(v) = conf.tracing {
            build.tracing(TraceExportConfig {
                filter: v.filter,
                exporter: TraceExporter::Otel(v.opentelemetry.try_into()?),
            });
        }
        if let Some(v) = conf.logging {
            build.logging(if v.forward {
                Logger::Forward { filter: v.filter }
            } else {
                Logger::Console { filter: v.filter }
            });
        }
        if let Some(v) = conf.metrics {
            build.metrics(if let Some(t) = v.opentelemetry {
                if v.prometheus.is_some() {
                    return Err(PyValueError::new_err(
                        "Cannot have OpenTelemetry and Prometheus metrics",
                    ));
                }
                MetricsExporter::Otel(t.try_into()?)
            } else if let Some(t) = v.prometheus {
                MetricsExporter::Prometheus(SocketAddr::from_str(&t.bind_address).map_err(
                    |err| PyValueError::new_err(format!("Invalid Prometheus address: {}", err)),
                )?)
            } else {
                return Err(PyValueError::new_err(
                    "Either OpenTelemetry or Prometheus config must be provided",
                ));
            });
        }
        build
            .build()
            .map_err(|err| PyValueError::new_err(format!("Invalid telemetry config: {}", err)))
    }
}

impl TryFrom<OpenTelemetryConfig> for OtelCollectorOptions {
    type Error = PyErr;

    fn try_from(conf: OpenTelemetryConfig) -> PyResult<Self> {
        Ok(OtelCollectorOptions {
            url: Url::parse(&conf.url)
                .map_err(|err| PyValueError::new_err(format!("Invalid OTel URL: {}", err)))?,
            headers: conf.headers,
            metric_periodicity: conf.metric_periodicity_millis.map(Duration::from_millis),
        })
    }
}

// Code below through the rest of the file is similar to
// https://github.com/awestlake87/pyo3-asyncio/blob/v0.16.0/src/tokio.rs but
// altered to support spawning based on current Tokio runtime instead of a
// single static one

struct TokioRuntime;

tokio::task_local! {
    static TASK_LOCALS: once_cell::unsync::OnceCell<pyo3_asyncio::TaskLocals>;
}

impl pyo3_asyncio::generic::Runtime for TokioRuntime {
    type JoinError = tokio::task::JoinError;
    type JoinHandle = tokio::task::JoinHandle<()>;

    fn spawn<F>(fut: F) -> Self::JoinHandle
    where
        F: Future<Output = ()> + Send + 'static,
    {
        tokio::runtime::Handle::current().spawn(async move {
            fut.await;
        })
    }
}

impl pyo3_asyncio::generic::ContextExt for TokioRuntime {
    fn scope<F, R>(
        locals: pyo3_asyncio::TaskLocals,
        fut: F,
    ) -> Pin<Box<dyn Future<Output = R> + Send>>
    where
        F: Future<Output = R> + Send + 'static,
    {
        let cell = once_cell::unsync::OnceCell::new();
        cell.set(locals).unwrap();

        Box::pin(TASK_LOCALS.scope(cell, fut))
    }

    fn get_task_locals() -> Option<pyo3_asyncio::TaskLocals> {
        match TASK_LOCALS.try_with(|c| c.get().map(|locals| locals.clone())) {
            Ok(locals) => locals,
            Err(_) => None,
        }
    }
}
