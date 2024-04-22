use pyo3::prelude::*;
use pyo3::types::PyTuple;

mod client;
mod metric;
mod runtime;
mod testing;
mod worker;

#[pymodule]
fn temporal_sdk_bridge(py: Python, m: &PyModule) -> PyResult<()> {
    // Client stuff
    m.add("RPCError", py.get_type::<client::RPCError>())?;
    m.add_class::<client::ClientRef>()?;
    m.add_function(wrap_pyfunction!(connect_client, m)?)?;

    // Metric stuff
    m.add_class::<metric::MetricMeterRef>()?;
    m.add_class::<metric::MetricAttributesRef>()?;
    m.add_class::<metric::MetricCounterRef>()?;
    m.add_class::<metric::MetricHistogramRef>()?;
    m.add_class::<metric::MetricHistogramFloatRef>()?;
    m.add_class::<metric::MetricHistogramDurationRef>()?;
    m.add_class::<metric::MetricGaugeRef>()?;
    m.add_class::<metric::MetricGaugeFloatRef>()?;
    m.add_class::<metric::BufferedMetricUpdate>()?;
    m.add_class::<metric::BufferedMetric>()?;
    m.add_function(wrap_pyfunction!(new_metric_meter, m)?)?;

    // Runtime stuff
    m.add_class::<runtime::RuntimeRef>()?;
    m.add_class::<runtime::BufferedLogEntry>()?;
    m.add_function(wrap_pyfunction!(init_runtime, m)?)?;
    m.add_function(wrap_pyfunction!(raise_in_thread, m)?)?;

    // Testing stuff
    m.add_class::<testing::EphemeralServerRef>()?;
    m.add_function(wrap_pyfunction!(start_dev_server, m)?)?;
    m.add_function(wrap_pyfunction!(start_test_server, m)?)?;

    // Worker stuff
    m.add(
        "PollShutdownError",
        py.get_type::<worker::PollShutdownError>(),
    )?;
    m.add_class::<worker::WorkerRef>()?;
    m.add_class::<worker::HistoryPusher>()?;
    m.add_function(wrap_pyfunction!(new_worker, m)?)?;
    m.add_function(wrap_pyfunction!(new_replay_worker, m)?)?;
    Ok(())
}

#[pyfunction]
fn connect_client<'a>(
    py: Python<'a>,
    runtime_ref: &runtime::RuntimeRef,
    config: client::ClientConfig,
) -> PyResult<&'a PyAny> {
    client::connect_client(py, &runtime_ref, config)
}

#[pyfunction]
fn new_metric_meter(runtime_ref: &runtime::RuntimeRef) -> Option<metric::MetricMeterRef> {
    metric::new_metric_meter(&runtime_ref)
}

#[pyfunction]
fn init_runtime(telemetry_config: runtime::TelemetryConfig) -> PyResult<runtime::RuntimeRef> {
    runtime::init_runtime(telemetry_config)
}

#[pyfunction]
fn raise_in_thread<'a>(py: Python<'a>, thread_id: std::os::raw::c_long, exc: &PyAny) -> bool {
    runtime::raise_in_thread(py, thread_id, exc)
}

#[pyfunction]
fn start_dev_server<'a>(
    py: Python<'a>,
    runtime_ref: &runtime::RuntimeRef,
    config: testing::DevServerConfig,
) -> PyResult<&'a PyAny> {
    testing::start_dev_server(py, &runtime_ref, config)
}

#[pyfunction]
fn start_test_server<'a>(
    py: Python<'a>,
    runtime_ref: &runtime::RuntimeRef,
    config: testing::TestServerConfig,
) -> PyResult<&'a PyAny> {
    testing::start_test_server(py, &runtime_ref, config)
}

#[pyfunction]
fn new_worker(
    runtime_ref: &runtime::RuntimeRef,
    client: &client::ClientRef,
    config: worker::WorkerConfig,
) -> PyResult<worker::WorkerRef> {
    worker::new_worker(&runtime_ref, &client, config)
}

#[pyfunction]
fn new_replay_worker<'a>(
    py: Python<'a>,
    runtime_ref: &runtime::RuntimeRef,
    config: worker::WorkerConfig,
) -> PyResult<&'a PyTuple> {
    worker::new_replay_worker(py, &runtime_ref, config)
}
