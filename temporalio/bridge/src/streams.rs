//! Bindings to the Stream runtime of the `temporalio-streams` crate, on the memory Store or, with
//! the `streams-redis` feature, on the Redis Store of `temporalio-streams-redis`. Python calls the
//! five Stream service operations by name, with the encoded request, and gets the encoded response.

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
    streams: std::sync::Arc<temporalio_streams::StreamRuntime>,
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
    last_write_ttl: Option<std::time::Duration>,
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
            last_write_ttl,
        },
        meter,
    );
    Ok(StreamRuntimeRef { streams, runtime })
}

/// The options of the Redis Store, as Python passes them. A `None` takes the crate's default.
#[cfg(feature = "streams-redis")]
pub struct RedisStreamOptions {
    pub urls: Vec<String>,
    pub cluster: bool,
    pub key_prefix: Option<String>,
    pub retention: Option<std::time::Duration>,
    pub last_write_ttl: Option<std::time::Duration>,
    pub response_timeout: Option<std::time::Duration>,
    pub store_name: String,
    pub wait_replicas: i32,
    pub wait_timeout: Option<std::time::Duration>,
}

/// Builds the runtime on Redis. It connects at its first call, so a server that is down fails that
/// call and not this one. A bad option raises `ValueError` with the crate's message.
#[cfg(feature = "streams-redis")]
pub fn new_redis_stream_runtime(
    runtime_ref: &runtime::RuntimeRef,
    options: RedisStreamOptions,
) -> PyResult<StreamRuntimeRef> {
    use pyo3::exceptions::PyValueError;
    use temporalio_streams_redis::{proto::RedisStreamConfig, RedisStreamRuntime, RuntimeOptions};

    let runtime = runtime_ref.runtime.clone();
    runtime.assert_same_process("create a Stream runtime")?;
    // The crate reads an empty or zero field as its default, so `None` maps to that.
    let config = RedisStreamConfig {
        urls: options.urls,
        cluster: options.cluster,
        key_prefix: options.key_prefix.unwrap_or_default(),
        retention: proto_duration("retention", options.retention)?,
        last_write_ttl: proto_duration("last_write_ttl", options.last_write_ttl)?,
        response_timeout: proto_duration("response_timeout", options.response_timeout)?,
        store_name: options.store_name,
        wait_replicas: options.wait_replicas,
        wait_timeout: proto_duration("wait_timeout", options.wait_timeout)?,
        ..Default::default()
    };
    let options =
        RuntimeOptions::try_from(config).map_err(|err| PyValueError::new_err(err.to_string()))?;
    // The runtime can spawn Tokio tasks, which need an entered Tokio runtime.
    let _guard = runtime.core.tokio_handle().enter();
    let meter = runtime.core.telemetry().get_temporal_metric_meter();
    let streams = RedisStreamRuntime::create(options, meter);
    Ok(StreamRuntimeRef { streams, runtime })
}

// The config's durations are the `prost-wkt-types` type that the Redis crate's protos use. The
// bridge doesn't depend on that crate, so the field's type picks the conversion.
#[cfg(feature = "streams-redis")]
fn proto_duration<T>(field: &str, value: Option<std::time::Duration>) -> PyResult<Option<T>>
where
    T: TryFrom<std::time::Duration>,
    T::Error: std::fmt::Display,
{
    value.map(T::try_from).transpose().map_err(|err| {
        pyo3::exceptions::PyValueError::new_err(format!("The {field} is too long: {err}"))
    })
}
