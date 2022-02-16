use pyo3::prelude::*;

mod client;
mod worker;

#[pymodule]
fn temporal_sdk_bridge(py: Python, m: &PyModule) -> PyResult<()> {
    // Client stuff
    m.add("RPCError", py.get_type::<client::RPCError>())?;
    m.add_class::<client::ClientRef>()?;
    m.add_function(wrap_pyfunction!(connect_client, m)?)?;

    // Worker stuff
    m.add("PollShutdownError", py.get_type::<worker::PollShutdownError>())?;
    m.add_class::<worker::WorkerRef>()?;
    m.add_function(wrap_pyfunction!(new_worker, m)?)?;
    Ok(())
}


#[pyfunction]
fn connect_client(py: Python, opts: client::ClientOptions) -> PyResult<&PyAny> {
    client::connect_client(py, opts)
}


#[pyfunction]
pub fn new_worker(client: &client::ClientRef, config: worker::WorkerConfig) -> PyResult<worker::WorkerRef> {
    worker::new_worker(&client, config)
}