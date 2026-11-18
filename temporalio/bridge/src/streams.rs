//! The adapter that lets Core call a Stream Store written in Python for Workflow code.

use pyo3::prelude::*;

use crate::runtime;

/// The crate's runtime object. Each Store written in Python has one.
#[pyclass(frozen)]
pub struct StreamRuntimeRef {
    pub(crate) streams: std::sync::Arc<temporalio_streams::StreamRuntime>,
}

/// Builds the runtime on a Store written in Python. Core calls the Store through it for Workflow
/// code, and the runtime checks each request and answer. `caller` runs each call of the Store on
/// the Worker's event loop, never on a Workflow thread.
pub fn new_python_stream_runtime(
    runtime_ref: &runtime::RuntimeRef,
    caller: Py<PyAny>,
) -> PyResult<StreamRuntimeRef> {
    let runtime = &runtime_ref.runtime;
    runtime.assert_same_process("create a Stream runtime")?;
    let meter = runtime.core.telemetry().get_temporal_metric_meter();
    let store = std::sync::Arc::new(python_store::PythonStore { caller });
    let streams = temporalio_streams::StreamRuntime::new(store, meter);
    Ok(StreamRuntimeRef { streams })
}

mod python_store {
    use futures::channel::oneshot;
    use prost::Message;
    use pyo3::prelude::*;
    use pyo3::types::PyBytes;
    use std::sync::Mutex;
    use std::time::Duration;
    use temporalio_streams::{
        proto::{
            AppendRecordsRequest, AppendRecordsResponse, CloseStreamRequest, CloseStreamResponse,
            CreateStreamRequest, CreateStreamResponse, DescribeStreamRequest,
            DescribeStreamResponse, ReadRecordsRequest, ReadRecordsResponse,
        },
        Reason, StreamError, StreamResult, StreamStore,
    };

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
                Ok(Err((value, message))) => match Reason::try_from(value) {
                    Ok(reason) => Err(StreamError::new(reason, message)),
                    Err(_) => Err(StreamError::storage_corrupt(format!(
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
    impl StreamStore for PythonStore {
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
