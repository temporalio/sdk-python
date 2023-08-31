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
use temporal_sdk_core::telemetry::{build_otlp_metric_exporter, start_prometheus_metric_exporter};
use temporal_sdk_core::CoreRuntime;
use temporal_sdk_core_api::telemetry::metrics::CoreMeter;
use temporal_sdk_core_api::telemetry::{
    Logger, MetricTemporality, OtelCollectorOptionsBuilder, PrometheusExporterOptionsBuilder,
    TelemetryOptions, TelemetryOptionsBuilder,
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
    logging: Option<LoggingConfig>,
    metrics: Option<MetricsConfig>,
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
    attach_service_name: bool,
    global_tags: Option<HashMap<String, String>>,
    metric_prefix: Option<String>,
}

#[derive(FromPyObject)]
pub struct OpenTelemetryConfig {
    url: String,
    headers: HashMap<String, String>,
    metric_periodicity_millis: Option<u64>,
    metric_temporality_delta: bool,
}

#[derive(FromPyObject)]
pub struct PrometheusConfig {
    bind_address: String,
    counters_total_suffix: bool,
    unit_suffix: bool,
}

pub fn init_runtime(telemetry_config: TelemetryConfig) -> PyResult<RuntimeRef> {
    let mut core = CoreRuntime::new(
        // We don't move telemetry config here because we need it for
        // late-binding metrics
        (&telemetry_config).try_into()?,
        tokio::runtime::Builder::new_multi_thread(),
    )
    .map_err(|err| PyRuntimeError::new_err(format!("Failed initializing telemetry: {}", err)))?;
    // We late-bind the metrics after core runtime is created since it needs
    // the Tokio handle
    if let Some(metrics_conf) = telemetry_config.metrics {
        let _guard = core.tokio_handle().enter();
        core.telemetry_mut()
            .attach_late_init_metrics(metrics_conf.try_into()?);
    }
    Ok(RuntimeRef {
        runtime: Runtime {
            core: Arc::new(core),
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

impl TryFrom<&TelemetryConfig> for TelemetryOptions {
    type Error = PyErr;

    fn try_from(conf: &TelemetryConfig) -> PyResult<Self> {
        let mut build = TelemetryOptionsBuilder::default();
        if let Some(logging_conf) = &conf.logging {
            build.logging(if logging_conf.forward {
                Logger::Forward {
                    filter: logging_conf.filter.to_string(),
                }
            } else {
                Logger::Console {
                    filter: logging_conf.filter.to_string(),
                }
            });
        }
        if let Some(metrics_conf) = &conf.metrics {
            // Note, actual metrics instance is late-bound in init_runtime
            build.attach_service_name(metrics_conf.attach_service_name);
            if let Some(prefix) = &metrics_conf.metric_prefix {
                build.metric_prefix(prefix.to_string());
            }
        }
        build
            .build()
            .map_err(|err| PyValueError::new_err(format!("Invalid telemetry config: {}", err)))
    }
}

impl TryFrom<MetricsConfig> for Arc<dyn CoreMeter> {
    type Error = PyErr;

    fn try_from(conf: MetricsConfig) -> PyResult<Self> {
        if let Some(otel_conf) = conf.opentelemetry {
            if !conf.prometheus.is_none() {
                return Err(PyValueError::new_err(
                    "Cannot have OpenTelemetry and Prometheus metrics",
                ));
            }

            // Build OTel exporter
            let mut build = OtelCollectorOptionsBuilder::default();
            build
                .url(
                    Url::parse(&otel_conf.url).map_err(|err| {
                        PyValueError::new_err(format!("Invalid OTel URL: {}", err))
                    })?,
                )
                .headers(otel_conf.headers);
            if let Some(period) = otel_conf.metric_periodicity_millis {
                build.metric_periodicity(Duration::from_millis(period));
            }
            if otel_conf.metric_temporality_delta {
                build.metric_temporality(MetricTemporality::Delta);
            }
            if let Some(global_tags) = conf.global_tags {
                build.global_tags(global_tags);
            }
            let otel_options = build
                .build()
                .map_err(|err| PyValueError::new_err(format!("Invalid OTel config: {}", err)))?;
            Ok(Arc::new(build_otlp_metric_exporter(otel_options).map_err(
                |err| PyValueError::new_err(format!("Failed building OTel exporter: {}", err)),
            )?))
        } else if let Some(prom_conf) = conf.prometheus {
            // Start prom exporter
            let mut build = PrometheusExporterOptionsBuilder::default();
            build
                .socket_addr(
                    SocketAddr::from_str(&prom_conf.bind_address).map_err(|err| {
                        PyValueError::new_err(format!("Invalid Prometheus address: {}", err))
                    })?,
                )
                .counters_total_suffix(prom_conf.counters_total_suffix)
                .unit_suffix(prom_conf.unit_suffix);
            if let Some(global_tags) = conf.global_tags {
                build.global_tags(global_tags);
            }
            let prom_options = build.build().map_err(|err| {
                PyValueError::new_err(format!("Invalid Prometheus config: {}", err))
            })?;
            Ok(start_prometheus_metric_exporter(prom_options)
                .map_err(|err| {
                    PyValueError::new_err(format!("Failed starting Prometheus exporter: {}", err))
                })?
                .meter)
        } else {
            Err(PyValueError::new_err(
                "Either OpenTelemetry or Prometheus config must be provided",
            ))
        }
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
