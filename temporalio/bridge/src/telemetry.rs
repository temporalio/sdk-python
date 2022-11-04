use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use std::collections::HashMap;
use std::net::SocketAddr;
use std::str::FromStr;
use temporal_sdk_core::{
    telemetry_init, Logger, MetricsExporter, OtelCollectorOptions, TelemetryOptions,
    TelemetryOptionsBuilder, TraceExportConfig, TraceExporter,
};
use url::Url;

#[pyclass]
pub struct TelemetryRef {
    // TODO(cretz): This is private
    // telemetry: &'static temporal_sdk_core::telemetry::GlobalTelemDat,
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
}

#[derive(FromPyObject)]
pub struct PrometheusConfig {
    bind_address: String,
}

pub fn init_telemetry(config: TelemetryConfig) -> PyResult<TelemetryRef> {
    let opts: TelemetryOptions = config.try_into()?;
    telemetry_init(&opts).map_err(|err| {
        PyRuntimeError::new_err(format!("Failed initializing telemetry: {}", err))
    })?;
    Ok(TelemetryRef {
        // telemetry: telem_dat,
    })
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
        })
    }
}
