use prost::Message;
use pyo3::exceptions::{PyException, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyTuple};
use std::sync::Arc;
use std::time::Duration;
use temporal_sdk_core::api::errors::{PollActivityError, PollWfError};
use temporal_sdk_core::replay::HistoryForReplay;
use temporal_sdk_core_api::Worker;
use temporal_sdk_core_protos::coresdk::workflow_completion::WorkflowActivationCompletion;
use temporal_sdk_core_protos::coresdk::{ActivityHeartbeat, ActivityTaskCompletion};
use temporal_sdk_core_protos::temporal::api::history::v1::History;
use tokio::sync::mpsc::{channel, Sender};
use tokio_stream::wrappers::ReceiverStream;

use crate::client;
use crate::runtime;

pyo3::create_exception!(temporal_sdk_bridge, PollShutdownError, PyException);

#[pyclass]
pub struct WorkerRef {
    worker: Option<Arc<temporal_sdk_core::Worker>>,
    runtime: runtime::Runtime,
}

#[derive(FromPyObject)]
pub struct WorkerConfig {
    namespace: String,
    task_queue: String,
    build_id: String,
    identity_override: Option<String>,
    max_cached_workflows: usize,
    max_outstanding_workflow_tasks: usize,
    max_outstanding_activities: usize,
    max_outstanding_local_activities: usize,
    max_concurrent_workflow_task_polls: usize,
    nonsticky_to_sticky_poll_ratio: f32,
    max_concurrent_activity_task_polls: usize,
    no_remote_activities: bool,
    sticky_queue_schedule_to_start_timeout_millis: u64,
    max_heartbeat_throttle_interval_millis: u64,
    default_heartbeat_throttle_interval_millis: u64,
    max_activities_per_second: Option<f64>,
    max_task_queue_activities_per_second: Option<f64>,
}

macro_rules! enter_sync {
    ($runtime:expr) => {
        temporal_sdk_core::telemetry::set_trace_subscriber_for_current_thread(
            $runtime.core.trace_subscriber(),
        );
        let _guard = $runtime.core.tokio_handle().enter();
    };
}

pub fn new_worker(
    runtime_ref: &runtime::RuntimeRef,
    client: &client::ClientRef,
    config: WorkerConfig,
) -> PyResult<WorkerRef> {
    enter_sync!(runtime_ref.runtime);
    let config: temporal_sdk_core::WorkerConfig = config.try_into()?;
    let worker = temporal_sdk_core::init_worker(
        &runtime_ref.runtime.core,
        config,
        client.retry_client.clone().into_inner(),
    )
    .map_err(|err| PyValueError::new_err(format!("Failed creating worker: {}", err)))?;
    Ok(WorkerRef {
        worker: Some(Arc::new(worker)),
        runtime: runtime_ref.runtime.clone(),
    })
}

pub fn new_replay_worker<'a>(
    py: Python<'a>,
    runtime_ref: &runtime::RuntimeRef,
    config: WorkerConfig,
) -> PyResult<&'a PyTuple> {
    enter_sync!(runtime_ref.runtime);
    let config: temporal_sdk_core::WorkerConfig = config.try_into()?;
    let (history_pusher, stream) = HistoryPusher::new(runtime_ref.runtime.clone());
    let worker = WorkerRef {
        worker: Some(Arc::new(
            temporal_sdk_core::init_replay_worker(config, stream).map_err(|err| {
                PyValueError::new_err(format!("Failed creating replay worker: {}", err))
            })?,
        )),
        runtime: runtime_ref.runtime.clone(),
    };
    Ok(PyTuple::new(
        py,
        [worker.into_py(py), history_pusher.into_py(py)],
    ))
}

#[pymethods]
impl WorkerRef {
    fn poll_workflow_activation<'p>(&self, py: Python<'p>) -> PyResult<&'p PyAny> {
        let worker = self.worker.as_ref().unwrap().clone();
        self.runtime.future_into_py(py, async move {
            let bytes = match worker.poll_workflow_activation().await {
                Ok(act) => act.encode_to_vec(),
                Err(PollWfError::ShutDown) => return Err(PollShutdownError::new_err(())),
                Err(err) => return Err(PyRuntimeError::new_err(format!("Poll failure: {}", err))),
            };
            let bytes: &[u8] = &bytes;
            Ok(Python::with_gil(|py| bytes.into_py(py)))
        })
    }

    fn poll_activity_task<'p>(&self, py: Python<'p>) -> PyResult<&'p PyAny> {
        let worker = self.worker.as_ref().unwrap().clone();
        self.runtime.future_into_py(py, async move {
            let bytes = match worker.poll_activity_task().await {
                Ok(task) => task.encode_to_vec(),
                Err(PollActivityError::ShutDown) => return Err(PollShutdownError::new_err(())),
                Err(err) => return Err(PyRuntimeError::new_err(format!("Poll failure: {}", err))),
            };
            let bytes: &[u8] = &bytes;
            Ok(Python::with_gil(|py| bytes.into_py(py)))
        })
    }

    fn complete_workflow_activation<'p>(
        &self,
        py: Python<'p>,
        proto: &PyBytes,
    ) -> PyResult<&'p PyAny> {
        let worker = self.worker.as_ref().unwrap().clone();
        let completion = WorkflowActivationCompletion::decode(proto.as_bytes())
            .map_err(|err| PyValueError::new_err(format!("Invalid proto: {}", err)))?;
        self.runtime.future_into_py(py, async move {
            worker
                .complete_workflow_activation(completion)
                .await
                .map_err(|err| {
                    // TODO(cretz): More error types
                    PyRuntimeError::new_err(format!("Completion failure: {}", err))
                })
        })
    }

    fn complete_activity_task<'p>(&self, py: Python<'p>, proto: &PyBytes) -> PyResult<&'p PyAny> {
        let worker = self.worker.as_ref().unwrap().clone();
        let completion = ActivityTaskCompletion::decode(proto.as_bytes())
            .map_err(|err| PyValueError::new_err(format!("Invalid proto: {}", err)))?;
        self.runtime.future_into_py(py, async move {
            worker
                .complete_activity_task(completion)
                .await
                .map_err(|err| {
                    // TODO(cretz): More error types
                    PyRuntimeError::new_err(format!("Completion failure: {}", err))
                })
        })
    }

    fn record_activity_heartbeat(&self, proto: &PyBytes) -> PyResult<()> {
        enter_sync!(self.runtime);
        let heartbeat = ActivityHeartbeat::decode(proto.as_bytes())
            .map_err(|err| PyValueError::new_err(format!("Invalid proto: {}", err)))?;
        self.worker
            .as_ref()
            .unwrap()
            .record_activity_heartbeat(heartbeat);
        Ok(())
    }

    fn request_workflow_eviction(&self, run_id: &str) -> PyResult<()> {
        enter_sync!(self.runtime);
        self.worker
            .as_ref()
            .unwrap()
            .request_workflow_eviction(run_id);
        Ok(())
    }

    fn shutdown<'p>(&self, py: Python<'p>) -> PyResult<&'p PyAny> {
        let worker = self.worker.as_ref().unwrap().clone();
        self.runtime.future_into_py(py, async move {
            worker.shutdown().await;
            Ok(())
        })
    }

    fn finalize_shutdown<'p>(&mut self, py: Python<'p>) -> PyResult<&'p PyAny> {
        // Take the worker out of the option and leave None. This should be the
        // only reference remaining to the worker so try_unwrap will work.
        let worker = Arc::try_unwrap(self.worker.take().unwrap()).map_err(|arc| {
            PyValueError::new_err(format!(
                "Cannot finalize, expected 1 reference, got {}",
                Arc::strong_count(&arc)
            ))
        })?;
        self.runtime.future_into_py(py, async move {
            worker.finalize_shutdown().await;
            Ok(())
        })
    }
}

impl TryFrom<WorkerConfig> for temporal_sdk_core::WorkerConfig {
    type Error = PyErr;

    fn try_from(conf: WorkerConfig) -> PyResult<Self> {
        temporal_sdk_core::WorkerConfigBuilder::default()
            .namespace(conf.namespace)
            .task_queue(conf.task_queue)
            .worker_build_id(conf.build_id)
            .client_identity_override(conf.identity_override)
            .max_cached_workflows(conf.max_cached_workflows)
            .max_outstanding_workflow_tasks(conf.max_outstanding_workflow_tasks)
            .max_outstanding_activities(conf.max_outstanding_activities)
            .max_outstanding_local_activities(conf.max_outstanding_local_activities)
            .max_concurrent_wft_polls(conf.max_concurrent_workflow_task_polls)
            .nonsticky_to_sticky_poll_ratio(conf.nonsticky_to_sticky_poll_ratio)
            .max_concurrent_at_polls(conf.max_concurrent_activity_task_polls)
            .no_remote_activities(conf.no_remote_activities)
            .sticky_queue_schedule_to_start_timeout(Duration::from_millis(
                conf.sticky_queue_schedule_to_start_timeout_millis,
            ))
            .max_heartbeat_throttle_interval(Duration::from_millis(
                conf.max_heartbeat_throttle_interval_millis,
            ))
            .default_heartbeat_throttle_interval(Duration::from_millis(
                conf.default_heartbeat_throttle_interval_millis,
            ))
            .max_worker_activities_per_second(conf.max_activities_per_second)
            .max_task_queue_activities_per_second(conf.max_task_queue_activities_per_second)
            .build()
            .map_err(|err| PyValueError::new_err(format!("Invalid worker config: {}", err)))
    }
}

/// For feeding histories into core during replay
#[pyclass]
pub struct HistoryPusher {
    tx: Option<Sender<HistoryForReplay>>,
    runtime: runtime::Runtime,
}

impl HistoryPusher {
    fn new(runtime: runtime::Runtime) -> (Self, ReceiverStream<HistoryForReplay>) {
        let (tx, rx) = channel(1);
        (
            Self {
                tx: Some(tx),
                runtime,
            },
            ReceiverStream::new(rx),
        )
    }
}

#[pymethods]
impl HistoryPusher {
    fn push_history<'p>(
        &self,
        py: Python<'p>,
        workflow_id: &str,
        history_proto: &PyBytes,
    ) -> PyResult<&'p PyAny> {
        let history = History::decode(history_proto.as_bytes())
            .map_err(|err| PyValueError::new_err(format!("Invalid proto: {}", err)))?;
        let wfid = workflow_id.to_string();
        let tx = if let Some(tx) = self.tx.as_ref() {
            tx.clone()
        } else {
            return Err(PyRuntimeError::new_err(
                "Replay worker is no longer accepting new histories",
            ));
        };
        // We accept this doesn't have logging/tracing
        self.runtime.future_into_py(py, async move {
            tx.send(HistoryForReplay::new(history, wfid))
                .await
                .map_err(|_| {
                    PyRuntimeError::new_err(
                        "Channel for history replay was dropped, this is an SDK bug.",
                    )
                })?;
            Ok(())
        })
    }

    fn close(&mut self) {
        self.tx.take();
    }
}
