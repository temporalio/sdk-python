use pyo3::prelude::*;
use pyo3::types::PyBytes;

mod client;
mod telemetry;
mod testing;
mod worker;

#[pymodule]
fn temporal_sdk_bridge(py: Python, m: &PyModule) -> PyResult<()> {
    // Client stuff
    m.add("RPCError", py.get_type::<client::RPCError>())?;
    m.add_class::<client::ClientRef>()?;
    m.add_function(wrap_pyfunction!(connect_client, m)?)?;

    // Telemetry stuff
    m.add_class::<telemetry::TelemetryRef>()?;
    m.add_function(wrap_pyfunction!(init_telemetry, m)?)?;

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
    m.add_function(wrap_pyfunction!(new_worker, m)?)?;
    m.add_function(wrap_pyfunction!(new_replay_worker, m)?)?;
    Ok(())
}

#[pyfunction]
fn connect_client(py: Python, config: client::ClientConfig) -> PyResult<&PyAny> {
    client::connect_client(py, config)
}

#[pyfunction]
fn init_telemetry(config: telemetry::TelemetryConfig) -> PyResult<telemetry::TelemetryRef> {
    telemetry::init_telemetry(config)
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
    client: &client::ClientRef,
    config: worker::WorkerConfig,
) -> PyResult<worker::WorkerRef> {
    worker::new_worker(&client, config)
}

#[pyfunction]
fn new_replay_worker(
    history_proto: &PyBytes,
    config: worker::WorkerConfig,
) -> PyResult<worker::WorkerRef> {
    worker::new_replay_worker(&history_proto, config)
}
