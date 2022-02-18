use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use std::str::FromStr;

#[pyclass]
pub struct TelemetryRef {
    // TODO(cretz): This is private
// telemetry: &'static temporal_sdk_core::telemetry::GlobalTelemDat,
}

#[derive(FromPyObject)]
pub struct TelemetryConfig {
    otel_collector_url: Option<String>,
    tracing_filter: Option<String>,
    log_forwarding_level: Option<String>,
    prometheus_export_bind_address: Option<String>,
}

pub fn init_telemetry(config: TelemetryConfig) -> PyResult<TelemetryRef> {
    let opts: temporal_sdk_core::TelemetryOptions = config.try_into()?;
    temporal_sdk_core::telemetry_init(&opts).map_err(|err| {
        PyRuntimeError::new_err(format!("Failed initializing telemetry: {}", err))
    })?;
    Ok(TelemetryRef {
        // telemetry: telem_dat,
    })
}

impl TryFrom<TelemetryConfig> for temporal_sdk_core::TelemetryOptions {
    type Error = PyErr;

    fn try_from(conf: TelemetryConfig) -> PyResult<Self> {
        let mut build = temporal_sdk_core::TelemetryOptionsBuilder::default();
        if let Some(ref v) = conf.otel_collector_url {
            build.otel_collector_url(
                url::Url::parse(v)
                    .map_err(|err| PyValueError::new_err(format!("Invalid OTel URL: {}", err)))?,
            );
        }
        if let Some(v) = conf.tracing_filter {
            build.tracing_filter(v);
        }
        if let Some(ref v) = conf.log_forwarding_level {
            build.log_forwarding_level(
                log::LevelFilter::from_str(v)
                    .map_err(|err| PyValueError::new_err(format!("Invalid log level: {}", err)))?,
            );
        }
        if let Some(ref v) = conf.prometheus_export_bind_address {
            build.prometheus_export_bind_address(std::net::SocketAddr::from_str(v).map_err(
                |err| PyValueError::new_err(format!("Invalid Prometheus address: {}", err)),
            )?);
        }
        build
            .build()
            .map_err(|err| PyValueError::new_err(format!("Invalid telemetry config: {}", err)))
    }
}
