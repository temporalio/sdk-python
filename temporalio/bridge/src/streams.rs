//! Bindings to the Stream runtime of the `temporalio-streams` crate, on the memory Store or on a
//! Store written in Python. Python calls the five Stream service operations by name, with the
//! encoded request, and gets the encoded response.

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

/// Builds the runtime on a Store written in Python. Core calls it for Workflow code through the
/// runtime, which checks each request and answer as for a Store in Rust. `caller` runs each call
/// of the Store on the Worker's event loop, never on a Workflow thread.
#[cfg(feature = "streams")]
pub fn new_python_stream_runtime(
    runtime_ref: &runtime::RuntimeRef,
    store_name: String,
    caller: Py<PyAny>,
) -> PyResult<StreamRuntimeRef> {
    let runtime = runtime_ref.runtime.clone();
    runtime.assert_same_process("create a Stream runtime")?;
    let meter = runtime.core.telemetry().get_temporal_metric_meter();
    let store = std::sync::Arc::new(python_store::PythonStore { caller });
    let streams = temporalio_streams::StreamRuntime::with_operations(store_name, store, meter);
    Ok(StreamRuntimeRef { streams, runtime })
}

#[cfg(feature = "streams")]
mod python_store {
    use futures::channel::oneshot;
    use prost::Message;
    use pyo3::prelude::*;
    use pyo3::types::PyBytes;
    use std::sync::Mutex;
    use std::time::Duration;
    use temporalio_streams::{
        operations::OperationStore,
        proto::{
            AppendRecordsRequest, AppendRecordsResponse, CloseStreamRequest, CloseStreamResponse,
            CreateStreamRequest, CreateStreamResponse, DescribeStreamRequest,
            DescribeStreamResponse, ReadRecordsRequest, ReadRecordsResponse,
        },
        Reason, StreamError, StreamResult,
    };

    /// The reasons that a Stream failure can carry to Workflow code.
    const REASONS: [Reason; 15] = [
        Reason::NotFound,
        Reason::Expired,
        Reason::Closed,
        Reason::Fenced,
        Reason::SequenceTooLow,
        Reason::SeqDivergent,
        Reason::NotOwner,
        Reason::FormatTooNew,
        Reason::OffsetTooHigh,
        Reason::TooLarge,
        Reason::Invalid,
        Reason::StorageUnavailable,
        Reason::Unsupported,
        Reason::AlreadyExists,
        Reason::StorageCorrupt,
    ];

    /// The longest pause after an empty page that came before its wait ended. A Store that
    /// ignores `wait` then gets a read about ten times a second, and not in a busy loop.
    const EMPTY_PAGE_PAUSE: Duration = Duration::from_millis(100);

    /// The encoded response, or the reason and the message of a failure.
    type Answer = Result<Vec<u8>, (i32, String)>;

    /// A Store written in Python. Each call runs as a task on the Worker's event loop.
    pub(crate) struct PythonStore {
        /// The Python object that starts a call on the event loop and gives a canceller.
        pub(crate) caller: Py<PyAny>,
    }

    /// What Python calls with the answer of one call.
    #[pyclass(frozen)]
    struct StoreCallDone {
        tx: Mutex<Option<oneshot::Sender<Answer>>>,
    }

    #[pymethods]
    impl StoreCallDone {
        fn __call__(&self, response: Option<&[u8]>, reason: i32, message: String) {
            let tx = self.tx.lock().ok().and_then(|mut tx| tx.take());
            if let Some(tx) = tx {
                // Core may have stopped the call, and then nobody waits for its answer.
                let _ = tx.send(match response {
                    Some(bytes) => Ok(bytes.to_vec()),
                    None => Err((reason, message)),
                });
            }
        }
    }

    /// Cancels the Python task of a call that Core stopped, so that no task stays on the loop.
    struct CancelOnDrop(Option<Py<PyAny>>);

    impl Drop for CancelOnDrop {
        fn drop(&mut self) {
            if let Some(cancel) = self.0.take() {
                // An interpreter that is shutting down has no task left to cancel.
                let _ = Python::try_attach(|py| {
                    if let Err(err) = cancel.call0(py) {
                        tracing::warn!("Failed to cancel a call to a Stream Store: {err}");
                    }
                });
            }
        }
    }

    fn reason(value: i32) -> Option<Reason> {
        REASONS
            .into_iter()
            .find(|reason| reason.proto() as i32 == value)
    }

    impl PythonStore {
        async fn call<Req: Message, Resp: Message + Default>(
            &self,
            operation: &'static str,
            request: Req,
        ) -> StreamResult<Resp> {
            let (tx, rx) = oneshot::channel();
            let done = StoreCallDone {
                tx: Mutex::new(Some(tx)),
            };
            let cancel = Python::attach(|py| {
                let request = PyBytes::new(py, &request.encode_to_vec());
                self.caller
                    .call_method1(py, "start", (operation, request, done))
            })
            .map_err(|err| {
                StreamError::storage_unavailable(format!(
                    "The Worker could not call the Stream Store: {err}"
                ))
            })?;
            let mut canceller = CancelOnDrop(Some(cancel));
            let answer = rx.await;
            // The task ended, so there is nothing to cancel.
            canceller.0 = None;
            match answer {
                Ok(Ok(bytes)) => Resp::decode(bytes.as_slice()).map_err(|err| {
                    StreamError::storage_corrupt(format!(
                        "The Stream Store gave a bad {operation} response: {err}"
                    ))
                }),
                Ok(Err((value, message))) => match reason(value) {
                    Some(reason) => Err(StreamError::new(reason, message)),
                    None => Err(StreamError::storage_unavailable(format!(
                        "The Stream Store failed with the unknown reason {value}: {message}"
                    ))),
                },
                Err(_) => Err(StreamError::storage_unavailable(
                    "The event loop of the Worker stopped the call to the Stream Store",
                )),
            }
        }
    }

    #[async_trait::async_trait]
    impl OperationStore for PythonStore {
        async fn create_stream(
            &self,
            request: CreateStreamRequest,
        ) -> StreamResult<CreateStreamResponse> {
            self.call("CreateStream", request).await
        }

        async fn append_records(
            &self,
            request: AppendRecordsRequest,
        ) -> StreamResult<AppendRecordsResponse> {
            self.call("AppendRecords", request).await
        }

        async fn read_records(
            &self,
            request: ReadRecordsRequest,
        ) -> StreamResult<ReadRecordsResponse> {
            let wait = request
                .wait
                .and_then(|wait| Duration::try_from(wait).ok())
                .unwrap_or_default();
            let started = tokio::time::Instant::now();
            let page: ReadRecordsResponse = self.call("ReadRecords", request).await?;
            let left = wait.saturating_sub(started.elapsed());
            if page.records.is_empty() && !page.done && !left.is_zero() {
                tokio::time::sleep(left.min(EMPTY_PAGE_PAUSE)).await;
            }
            Ok(page)
        }

        async fn close_stream(
            &self,
            request: CloseStreamRequest,
        ) -> StreamResult<CloseStreamResponse> {
            self.call("CloseStream", request).await
        }

        async fn describe_stream(
            &self,
            request: DescribeStreamRequest,
        ) -> StreamResult<DescribeStreamResponse> {
            self.call("DescribeStream", request).await
        }
    }
}
