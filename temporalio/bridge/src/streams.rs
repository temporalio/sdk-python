use futures::future::BoxFuture;
use prost::Message;
use pyo3::exceptions::asyncio::CancelledError;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{IntoPyDict, PyBytes, PyType};
use std::sync::{Arc, Mutex, MutexGuard, OnceLock, PoisonError};
use std::time::Duration;
use temporalio_sdk_core::StreamRuntimes;
use temporalio_streams::proto::{
    AppendRecordsRequest, AppendRecordsResponse, CloseStreamRequest, CloseStreamResponse,
    CreateStreamRequest, CreateStreamResponse, DescribeStreamRequest, DescribeStreamResponse,
    ReadRecordsRequest, ReadRecordsResponse, StreamErrorReason,
};
use temporalio_streams::record::StoreLimits;
use temporalio_streams::{
    AppendError, CloseError, CreateError, DescribeError, ReadError, Reason, StreamError,
    StreamRuntime, StreamStore, WireReason,
};

use crate::runtime;

/// A Stream Store that is a Python object. `caller` is a `_StoreCaller` of
/// `temporalio.client._stream_store`, and its `call` runs on the event loop of the task locals.
struct PythonStore {
    caller: Py<PyAny>,
    event_loop_task_locals: Arc<OnceLock<pyo3_async_runtimes::TaskLocals>>,
}

/// The state of one call of a Python Store, which the call's guard and its `OnTask` share.
enum CallState {
    /// Python hasn't started the task of the call.
    Pending,
    /// The Python task that runs the call.
    Running(Py<PyAny>),
    /// The caller dropped the call before its answer.
    Dropped,
    /// The call has its answer.
    Done,
}

// Nothing calls into Python while it holds this lock, since `OnTask` takes it with the GIL held
// and the guard needs the GIL only after it let the lock go.
fn lock(state: &Mutex<CallState>) -> MutexGuard<'_, CallState> {
    state.lock().unwrap_or_else(PoisonError::into_inner)
}

/// The callback that takes the Python task of a call from `_StoreCaller.call`, which then calls
/// the Store.
#[pyclass]
struct OnTask {
    state: Arc<Mutex<CallState>>,
}

#[pymethods]
impl OnTask {
    fn __call__(&self, task: Py<PyAny>) -> PyResult<()> {
        let mut state = lock(&self.state);
        if matches!(*state, CallState::Dropped) {
            drop(state);
            return Err(CancelledError::new_err(()));
        }
        *state = CallState::Running(task);
        Ok(())
    }
}

/// The guard that cancels the Python task of a call that its caller drops before the answer. Core
/// drops a call when it stops a read, evicts a run, gives up a try or shuts down.
struct CallGuard<'a> {
    state: Arc<Mutex<CallState>>,
    task_locals: &'a pyo3_async_runtimes::TaskLocals,
}

impl Drop for CallGuard<'_> {
    fn drop(&mut self) {
        let state = std::mem::replace(&mut *lock(&self.state), CallState::Dropped);
        if let CallState::Running(task) = state {
            // The loop may be closed and the interpreter may be finalizing, and then there's no
            // task left to cancel. asyncio lets another thread only schedule the cancel.
            let _ = Python::try_attach(|py| {
                let cancel = task.getattr(py, "cancel")?;
                let event_loop = self.task_locals.event_loop(py);
                event_loop.call_method1("call_soon_threadsafe", (cancel,))?;
                PyResult::Ok(())
            });
        }
    }
}

impl PythonStore {
    async fn call<Request, Response, R>(
        &self,
        operation: &'static str,
        request: Request,
    ) -> Result<Response, StreamError<Reason<R>>>
    where
        Request: Message,
        Response: Message + Default,
        R: WireReason,
    {
        let Some(task_locals) = self.event_loop_task_locals.get() else {
            return Err(StreamError::store_unavailable(
                "The Worker hasn't set the event loop of its Stream Stores yet",
            ));
        };
        let state = Arc::new(Mutex::new(CallState::Pending));
        let _guard = CallGuard {
            state: state.clone(),
            task_locals,
        };
        let called = Python::attach(|py| {
            let on_task = OnTask {
                state: state.clone(),
            };
            let request = PyBytes::new(py, &request.encode_to_vec());
            let coroutine =
                (self.caller.bind(py)).call_method1("call", (operation, request, on_task))?;
            pyo3_async_runtimes::into_future_with_locals(task_locals, coroutine)
        });
        let answer = match called {
            Ok(answer) => answer.await,
            Err(err) => Err(err),
        };
        Python::attach(|py| {
            let _task = std::mem::replace(&mut *lock(&state), CallState::Done);
            match answer {
                Ok(response) => (response.bind(py).cast::<PyBytes>().ok())
                    .and_then(|bytes| Response::decode(bytes.as_bytes()).ok())
                    .ok_or_else(|| {
                        StreamError::store_corrupt(format!(
                            "The Store didn't answer the {operation} call with its encoded \
                             response message"
                        ))
                    }),
                Err(err) => Err(store_error(py, err)),
            }
        })
    }
}

/// Returns the error of an operation for the exception of a Store. A `StreamError` is a refusal
/// with its reason, and any other exception is an outage, which the caller can try again.
fn store_error<R: WireReason>(py: Python<'_>, err: PyErr) -> StreamError<Reason<R>> {
    let refusal = || -> PyResult<Option<(i32, String)>> {
        let value = err.value(py);
        if !value.is_instance(stream_error_type(py)?)? {
            return Ok(None);
        }
        let reason = value.getattr("reason")?.extract()?;
        Ok(Some((reason, value.getattr("message")?.extract()?)))
    };
    match refusal() {
        Ok(Some((reason, message))) => StreamError::from_wire(reason, message),
        _ => StreamError::store_unavailable(err.to_string()),
    }
}

fn stream_error_type(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    static STREAM_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
    STREAM_ERROR.import(py, "temporalio.exceptions", "StreamError")
}

impl StreamStore for PythonStore {
    fn create_stream(
        &self,
        request: CreateStreamRequest,
    ) -> BoxFuture<'_, Result<CreateStreamResponse, CreateError>> {
        Box::pin(self.call("CreateStream", request))
    }

    fn append_records(
        &self,
        request: AppendRecordsRequest,
    ) -> BoxFuture<'_, Result<AppendRecordsResponse, AppendError>> {
        Box::pin(self.call("AppendRecords", request))
    }

    fn read_records(
        &self,
        request: ReadRecordsRequest,
    ) -> BoxFuture<'_, Result<ReadRecordsResponse, ReadError>> {
        Box::pin(self.call("ReadRecords", request))
    }

    fn close_stream(
        &self,
        request: CloseStreamRequest,
    ) -> BoxFuture<'_, Result<CloseStreamResponse, CloseError>> {
        Box::pin(self.call("CloseStream", request))
    }

    fn describe_stream(
        &self,
        request: DescribeStreamRequest,
    ) -> BoxFuture<'_, Result<DescribeStreamResponse, DescribeError>> {
        Box::pin(self.call("DescribeStream", request))
    }
}

/// Calls the Python Store of `caller` for one Stream operation through a `StreamRuntime`, which
/// checks the request and the response. The awaitable raises `StreamError` for a failed call. The
/// runtime has no limit of a batch, since the SDK that calls the Store has those in its Stream
/// config.
#[pyfunction]
pub fn call_stream_store<'p>(
    py: Python<'p>,
    runtime_ref: &runtime::RuntimeRef,
    caller: Py<PyAny>,
    operation: &str,
    request: &Bound<'_, PyBytes>,
) -> PyResult<Bound<'p, PyAny>> {
    let task_locals = pyo3_async_runtimes::TaskLocals::with_running_loop(py)?.copy_context(py)?;
    let store = PythonStore {
        caller,
        event_loop_task_locals: Arc::new(OnceLock::from(task_locals)),
    };
    let stream_runtime = StreamRuntime::new(Arc::new(store), StoreLimits::UNBOUNDED);
    macro_rules! run {
        ($method:ident) => {{
            let request = Message::decode(request.as_bytes())
                .map_err(|err| PyValueError::new_err(format!("Invalid proto: {err}")))?;
            runtime_ref.runtime.future_into_py(py, async move {
                let response = stream_runtime.$method(request).await;
                response
                    .map(|response| response.encode_to_vec())
                    .map_err(|err| stream_error(err.reason.into(), err.message))
            })
        }};
    }
    match operation {
        "CreateStream" => run!(create_stream),
        "AppendRecords" => run!(append_records),
        "ReadRecords" => run!(read_records),
        "CloseStream" => run!(close_stream),
        "DescribeStream" => run!(describe_stream),
        _ => Err(PyValueError::new_err(format!(
            "Unknown Stream operation: {operation}"
        ))),
    }
}

/// Returns the limits of a Stream config, which Python gives as its `max_record_bytes`,
/// `max_batch_bytes` and `max_batch_records`.
fn store_limits(
    (max_record_bytes, max_batch_bytes, max_batch_records): (usize, usize, usize),
) -> PyResult<StoreLimits> {
    StoreLimits::default()
        .with_max_record_bytes(max_record_bytes)
        .and_then(|limits| limits.with_max_batch_bytes(max_batch_bytes))
        .and_then(|limits| limits.with_max_batch_records(max_batch_records))
        .map_err(|err| PyValueError::new_err(err.message))
}

/// The Stream Stores of a Worker, which Python gives as a `WorkerStreams` of
/// `temporalio.bridge.streams`.
#[derive(FromPyObject)]
pub(crate) struct WorkerStreams {
    limits: Option<(usize, usize, usize)>,
    stores: Vec<(String, Py<PyAny>)>,
    read_max_wait_millis: Option<u64>,
}

impl WorkerStreams {
    /// Returns the runtime of each Store by its name. Each Store runs its calls on the event
    /// loop of `task_locals`.
    pub(crate) fn into_runtimes(
        self,
        task_locals: &Arc<OnceLock<pyo3_async_runtimes::TaskLocals>>,
    ) -> PyResult<StreamRuntimes> {
        let limits = (self.limits.map(store_limits).transpose()?).unwrap_or_default();
        let runtimes = self.stores.into_iter().map(|(name, caller)| {
            let store = PythonStore {
                caller,
                event_loop_task_locals: task_locals.clone(),
            };
            let runtime = StreamRuntime::new(Arc::new(store), limits);
            (name, Arc::new(runtime))
        });
        let mut runtimes = StreamRuntimes::new(runtimes);
        if let Some(millis) = self.read_max_wait_millis {
            runtimes.settings.read_max_wait = Duration::from_millis(millis);
        }
        Ok(runtimes)
    }
}

/// Checks an encoded append of client code as the runtime of a Worker checks one of Workflow
/// code, with the limits of the Stream config. Client code reaches its Store through no such
/// runtime. Raises `StreamError` for an append that the check refuses.
#[pyfunction]
pub fn check_stream_append(
    limits: (usize, usize, usize),
    request: &Bound<'_, PyBytes>,
) -> PyResult<()> {
    let request = Message::decode(request.as_bytes())
        .map_err(|err| PyValueError::new_err(format!("Invalid proto: {err}")))?;
    store_limits(limits)?
        .check_append(&request)
        .map_err(|err| stream_error(err.reason.into(), err.message))
}

/// Returns a `temporalio.exceptions.StreamError`, or the error that building it raised.
fn stream_error(reason: StreamErrorReason, message: String) -> PyErr {
    Python::attach(|py| {
        let error = || {
            let reason = [("reason", reason as i32)].into_py_dict(py)?;
            stream_error_type(py)?.call((message,), Some(&reason))
        };
        error().map_or_else(|err| err, PyErr::from_value)
    })
}
