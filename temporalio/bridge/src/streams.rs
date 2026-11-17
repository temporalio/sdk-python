//! Bindings to the Stream runtime of the `temporalio-streams` crate, on the memory Store. Python
//! calls the five Stream service operations by name, with the encoded request, and gets the encoded
//! response.

use pyo3::exceptions::PyException;
#[cfg(feature = "streams")]
use pyo3::prelude::*;

#[cfg(feature = "streams")]
use crate::runtime;

// The exception args are the gRPC code, the message and the encoded details, as in `RPCError`.
pyo3::create_exception!(temporal_sdk_bridge, StreamCallError, PyException);

/// The crate's runtime object. Each Store on the bridge holds one.
#[cfg(feature = "streams")]
#[pyclass(frozen)]
pub struct StreamRuntimeRef {
    pub(crate) streams: std::sync::Arc<temporalio_streams::StreamRuntime>,
    runtime: runtime::Runtime,
}

#[cfg(feature = "streams")]
#[pymethods]
impl StreamRuntimeRef {
    /// Runs one Stream operation on an encoded request. The future gives the encoded response.
    fn call<'p>(
        &self,
        py: Python<'p>,
        operation: String,
        request: Vec<u8>,
    ) -> PyResult<Bound<'p, PyAny>> {
        self.runtime
            .assert_same_process("call the Stream runtime")?;
        let streams = self.streams.clone();
        self.runtime.future_into_py(py, async move {
            streams.call(&operation, &request).await.map_err(call_error)
        })
    }
}

#[cfg(feature = "streams")]
fn call_error(err: temporalio_streams::StreamError) -> PyErr {
    StreamCallError::new_err((err.code() as u32, err.message.clone(), err.details()))
}

/// Builds the runtime on a new memory Store. A `None` duration takes the crate's default.
#[cfg(feature = "streams")]
pub fn new_memory_stream_runtime(
    runtime_ref: &runtime::RuntimeRef,
    store_name: String,
    retention: Option<std::time::Duration>,
    producer_idle_ttl: Option<std::time::Duration>,
) -> PyResult<StreamRuntimeRef> {
    let runtime = runtime_ref.runtime.clone();
    runtime.assert_same_process("create a Stream runtime")?;
    // The runtime can spawn Tokio tasks, which need an entered Tokio runtime.
    let _guard = runtime.core.tokio_handle().enter();
    let meter = runtime.core.telemetry().get_temporal_metric_meter();
    let streams = temporalio_streams::MemoryStreamRuntime::new(
        store_name,
        temporalio_streams::MemoryStreamOptions {
            retention,
            producer_idle_ttl,
        },
        meter,
    );
    Ok(StreamRuntimeRef { streams, runtime })
}
