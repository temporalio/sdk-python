use std::{collections::HashMap, sync::Arc};

use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use temporal_sdk_core_api::telemetry::metrics;

use crate::runtime;

#[pyclass]
pub struct MetricMeterRef {
    meter: metrics::TemporalMeter,
    #[pyo3(get)]
    default_attributes: MetricAttributesRef,
}

#[pyclass]
#[derive(Clone)]
pub struct MetricAttributesRef {
    attrs: metrics::MetricAttributes,
}

#[pyclass]
pub struct MetricCounterRef {
    counter: Arc<dyn metrics::Counter>,
}

#[pyclass]
pub struct MetricHistogramRef {
    histogram: Arc<dyn metrics::Histogram>,
}

#[pyclass]
pub struct MetricGaugeRef {
    gauge: Arc<dyn metrics::Gauge>,
}

pub fn new_metric_meter(runtime_ref: &runtime::RuntimeRef) -> Option<MetricMeterRef> {
    runtime_ref
        .runtime
        .core
        .telemetry()
        .get_metric_meter()
        .map(|meter| {
            let default_attributes = MetricAttributesRef {
                attrs: meter.inner.new_attributes(meter.default_attribs.clone()),
            };
            MetricMeterRef {
                meter,
                default_attributes,
            }
        })
}

#[pymethods]
impl MetricMeterRef {
    fn new_counter(
        &self,
        name: String,
        description: Option<String>,
        unit: Option<String>,
    ) -> MetricCounterRef {
        MetricCounterRef {
            counter: self
                .meter
                .inner
                .counter(build_metric_parameters(name, description, unit)),
        }
    }

    fn new_histogram(
        &self,
        name: String,
        description: Option<String>,
        unit: Option<String>,
    ) -> MetricHistogramRef {
        MetricHistogramRef {
            histogram: self
                .meter
                .inner
                .histogram(build_metric_parameters(name, description, unit)),
        }
    }

    fn new_gauge(
        &self,
        name: String,
        description: Option<String>,
        unit: Option<String>,
    ) -> MetricGaugeRef {
        MetricGaugeRef {
            gauge: self
                .meter
                .inner
                .gauge(build_metric_parameters(name, description, unit)),
        }
    }
}

#[pymethods]
impl MetricCounterRef {
    fn add(&self, value: u64, attrs_ref: &MetricAttributesRef) {
        self.counter.add(value, &attrs_ref.attrs);
    }
}

#[pymethods]
impl MetricHistogramRef {
    fn record(&self, value: u64, attrs_ref: &MetricAttributesRef) {
        self.histogram.record(value, &attrs_ref.attrs);
    }
}

#[pymethods]
impl MetricGaugeRef {
    fn set(&self, value: u64, attrs_ref: &MetricAttributesRef) {
        self.gauge.record(value, &attrs_ref.attrs);
    }
}

fn build_metric_parameters(
    name: String,
    description: Option<String>,
    unit: Option<String>,
) -> metrics::MetricParameters {
    let mut build = metrics::MetricParametersBuilder::default();
    build.name(name);
    if let Some(description) = description {
        build.description(description);
    }
    if let Some(unit) = unit {
        build.unit(unit);
    }
    // Should be nothing that would fail validation here
    build.build().unwrap()
}

#[pymethods]
impl MetricAttributesRef {
    fn with_additional_attributes<'p>(
        &self,
        py: Python<'p>,
        new_attrs: HashMap<String, PyObject>,
    ) -> PyResult<Self> {
        let mut attrs = self.attrs.clone();
        attrs.add_new_attrs(
            new_attrs
                .into_iter()
                .map(|(k, obj)| metric_key_value_from_py(py, k, obj))
                .collect::<PyResult<Vec<metrics::MetricKeyValue>>>()?,
        );
        Ok(MetricAttributesRef { attrs })
    }
}

fn metric_key_value_from_py<'p>(
    py: Python<'p>,
    k: String,
    obj: PyObject,
) -> PyResult<metrics::MetricKeyValue> {
    let val = if let Ok(v) = obj.extract::<String>(py) {
        metrics::MetricValue::String(v)
    } else if let Ok(v) = obj.extract::<bool>(py) {
        metrics::MetricValue::Bool(v)
    } else if let Ok(v) = obj.extract::<i64>(py) {
        metrics::MetricValue::Int(v)
    } else if let Ok(v) = obj.extract::<f64>(py) {
        metrics::MetricValue::Float(v)
    } else {
        return Err(PyTypeError::new_err(format!(
            "Invalid value type for key {}, must be str, int, float, or bool",
            k
        )));
    };
    Ok(metrics::MetricKeyValue::new(k, val))
}
