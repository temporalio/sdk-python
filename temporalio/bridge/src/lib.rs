use pyo3::prelude::*;
use pyo3::types::PyTuple;

mod client;
mod runtime;
mod testing;
mod worker;

#[pymodule]
fn temporal_sdk_bridge(py: Python, m: &PyModule) -> PyResult<()> {
    // Client stuff
    m.add("RPCError", py.get_type::<client::RPCError>())?;
    m.add_class::<client::ClientRef>()?;
    m.add_function(wrap_pyfunction!(connect_client, m)?)?;

    // Runtime stuff
    m.add_class::<runtime::RuntimeRef>()?;
    m.add_function(wrap_pyfunction!(init_runtime, m)?)?;

    // Testing stuff
    m.add_class::<testing::EphemeralServerRef>()?;
    m.add_function(wrap_pyfunction!(start_temporalite, m)?)?;
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
    runtime: &runtime::RuntimeRef,
    config: client::ClientConfig,
) -> PyResult<&'a PyAny> {
    client::connect_client(py, &runtime, config)
}

#[pyfunction]
fn init_runtime(telemetry_config: runtime::TelemetryConfig) -> PyResult<runtime::RuntimeRef> {
    runtime::init_runtime(telemetry_config)
}

#[pyfunction]
fn start_temporalite(py: Python, config: testing::TemporaliteConfig) -> PyResult<&PyAny> {
    testing::start_temporalite(py, config)
}

#[pyfunction]
fn start_test_server(py: Python, config: testing::TestServerConfig) -> PyResult<&PyAny> {
    testing::start_test_server(py, config)
}

#[pyfunction]
fn new_worker(
    runtime: &runtime::RuntimeRef,
    client: &client::ClientRef,
    config: worker::WorkerConfig,
) -> PyResult<worker::WorkerRef> {
    worker::new_worker(&runtime, &client, config)
}

#[pyfunction]
fn new_replay_worker<'a>(
    py: Python<'a>,
    runtime: &runtime::RuntimeRef,
    config: worker::WorkerConfig,
) -> PyResult<&'a PyTuple> {
    worker::new_replay_worker(py, &runtime, config)
}
