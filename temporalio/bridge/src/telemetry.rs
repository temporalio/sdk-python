use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use std::collections::HashMap;
use std::net::SocketAddr;
use std::str::FromStr;
use temporal_sdk_core::{
    telemetry_init, Logger, MetricsExporter, OtelCollectorOptions, TelemetryOptions,
    TelemetryOptionsBuilder, TraceExporter,
};
use url::Url;

#[pyclass]
pub struct TelemetryRef {
    // TODO(cretz): This is private
    // telemetry: &'static temporal_sdk_core::telemetry::GlobalTelemDat,
}

#[derive(FromPyObject)]
pub struct TelemetryConfig {
    tracing_filter: Option<String>,
    otel_tracing: Option<OtelCollectorConfig>,
    log_console: bool,
    log_forwarding_level: Option<String>,
    otel_metrics: Option<OtelCollectorConfig>,
    prometheus_metrics: Option<PrometheusMetricsConfig>,
}

#[derive(FromPyObject)]
pub struct OtelCollectorConfig {
    url: String,
    headers: HashMap<String, String>,
}

#[derive(FromPyObject)]
pub struct PrometheusMetricsConfig {
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
        if let Some(v) = conf.tracing_filter {
            build.tracing_filter(v);
        }
        if let Some(v) = conf.otel_tracing {
            build.tracing(TraceExporter::Otel(v.try_into()?));
        }
        if let Some(ref v) = conf.log_forwarding_level {
            if conf.log_console {
                return Err(PyValueError::new_err(
                    "Cannot have log forwarding level and log console",
                ));
            }
            build.logging(Logger::Forward(log::LevelFilter::from_str(v).map_err(
                |err| PyValueError::new_err(format!("Invalid log level: {}", err)),
            )?));
        } else if conf.log_console {
            build.logging(Logger::Console);
        }
        if let Some(v) = conf.otel_metrics {
            if conf.prometheus_metrics.is_some() {
                return Err(PyValueError::new_err(
                    "Cannot have OTel and Prometheus metrics",
                ));
            }
            build.metrics(MetricsExporter::Otel(v.try_into()?));
        } else if let Some(v) = conf.prometheus_metrics {
            build.metrics(MetricsExporter::Prometheus(
                SocketAddr::from_str(&v.bind_address).map_err(|err| {
                    PyValueError::new_err(format!("Invalid Prometheus address: {}", err))
                })?,
            ));
        }
        build
            .build()
            .map_err(|err| PyValueError::new_err(format!("Invalid telemetry config: {}", err)))
    }
}

impl TryFrom<OtelCollectorConfig> for OtelCollectorOptions {
    type Error = PyErr;

    fn try_from(conf: OtelCollectorConfig) -> PyResult<Self> {
        Ok(OtelCollectorOptions {
            url: Url::parse(&conf.url)
                .map_err(|err| PyValueError::new_err(format!("Invalid OTel URL: {}", err)))?,
            headers: conf.headers,
        })
    }
}
