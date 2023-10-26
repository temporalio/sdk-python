use std::any::Any;
use std::{collections::HashMap, sync::Arc};

use pyo3::prelude::*;
use pyo3::{exceptions::PyTypeError, types::PyDict};
use temporal_sdk_core_api::telemetry::metrics::{
    self, BufferInstrumentRef, CustomMetricAttributes, MetricEvent, NewAttributes,
};

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
        meter: &MetricMeterRef,
        new_attrs: HashMap<String, PyObject>,
    ) -> PyResult<Self> {
        let attrs = meter.meter.inner.extend_attributes(
            self.attrs.clone(),
            NewAttributes {
                attributes: new_attrs
                    .into_iter()
                    .map(|(k, obj)| metric_key_value_from_py(py, k, obj))
                    .collect::<PyResult<Vec<metrics::MetricKeyValue>>>()?,
            },
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

// WARNING: This must match temporalio.runtime.BufferedMetricUpdate protocol
#[pyclass]
pub struct BufferedMetricUpdate {
    #[pyo3(get)]
    pub metric: Py<BufferedMetric>,
    #[pyo3(get)]
    pub value: u64,
    #[pyo3(get)]
    pub attributes: Py<PyDict>,
}

// WARNING: This must match temporalio.runtime.BufferedMetric protocol
#[pyclass]
pub struct BufferedMetric {
    #[pyo3(get)]
    pub name: String,
    #[pyo3(get)]
    pub description: Option<String>,
    #[pyo3(get)]
    pub unit: Option<String>,
    #[pyo3(get)]
    pub kind: u8, // 0 - counter, 1 - gauge, 2 - histogram
}

#[derive(Debug)]
struct BufferedMetricAttributes(Py<PyDict>);

#[derive(Clone, Debug)]
pub struct BufferedMetricRef(Py<BufferedMetric>);

impl BufferInstrumentRef for BufferedMetricRef {}

impl CustomMetricAttributes for BufferedMetricAttributes {
    fn as_any(self: Arc<Self>) -> Arc<dyn Any + Send + Sync> {
        self as Arc<dyn Any + Send + Sync>
    }
}

pub fn convert_metric_events<'p>(
    py: Python<'p>,
    events: Vec<MetricEvent<BufferedMetricRef>>,
) -> Vec<BufferedMetricUpdate> {
    events
        .into_iter()
        .filter_map(|e| convert_metric_event(py, e))
        .collect()
}

fn convert_metric_event<'p>(
    py: Python<'p>,
    event: MetricEvent<BufferedMetricRef>,
) -> Option<BufferedMetricUpdate> {
    match event {
        // Create the metric and put it on the lazy ref
        MetricEvent::Create {
            params,
            populate_into,
            kind,
        } => {
            let buffered_ref = BufferedMetricRef(
                Py::new(
                    py,
                    BufferedMetric {
                        name: params.name.to_string(),
                        description: Some(params.description)
                            .filter(|s| !s.is_empty())
                            .map(|s| s.to_string()),
                        unit: Some(params.unit)
                            .filter(|s| !s.is_empty())
                            .map(|s| s.to_string()),
                        kind: match kind {
                            metrics::MetricKind::Counter => 0,
                            metrics::MetricKind::Gauge => 1,
                            metrics::MetricKind::Histogram => 2,
                        },
                    },
                )
                .expect("Unable to create buffered metric"),
            );
            populate_into.set(Arc::new(buffered_ref)).unwrap();
            None
        }
        // Create the attributes and put it on the lazy ref
        MetricEvent::CreateAttributes {
            populate_into,
            append_from,
            attributes,
        } => {
            // Create the dictionary (as copy from existing if needed)
            let new_attrs_ref: Py<PyDict> = match append_from {
                Some(existing) => existing
                    .get()
                    .clone()
                    .as_any()
                    .downcast::<BufferedMetricAttributes>()
                    .expect("Unable to downcast to expected buffered metric attributes")
                    .0
                    .as_ref(py)
                    .copy()
                    .expect("Failed to copy metric attribute dictionary")
                    .into(),
                None => PyDict::new(py).into(),
            };
            // Add attributes
            let new_attrs = new_attrs_ref.as_ref(py);
            for kv in attributes.into_iter() {
                match kv.value {
                    metrics::MetricValue::String(v) => new_attrs.set_item(kv.key, v),
                    metrics::MetricValue::Int(v) => new_attrs.set_item(kv.key, v),
                    metrics::MetricValue::Float(v) => new_attrs.set_item(kv.key, v),
                    metrics::MetricValue::Bool(v) => new_attrs.set_item(kv.key, v),
                }
                .expect("Unable to set metric key/value on dictionary");
            }
            // Put on lazy ref
            populate_into
                .set(Arc::new(BufferedMetricAttributes(new_attrs_ref)))
                .expect("Unable to set buffered metric attributes on reference");
            None
        }
        // Convert to Python metric event
        MetricEvent::Update {
            instrument,
            attributes,
            update,
        } => Some(BufferedMetricUpdate {
            metric: instrument.get().clone().0.clone(),
            value: match update {
                metrics::MetricUpdateVal::Delta(v) => v,
                metrics::MetricUpdateVal::Value(v) => v,
            },
            attributes: attributes
                .get()
                .clone()
                .as_any()
                .downcast::<BufferedMetricAttributes>()
                .expect("Unable to downcast to expected buffered metric attributes")
                .0
                .clone(),
        }),
    }
}
