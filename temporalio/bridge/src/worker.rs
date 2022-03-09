use prost::Message;
use pyo3::exceptions::{PyException, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use pyo3_asyncio::tokio::future_into_py;
use std::sync::Arc;
use std::time::Duration;
use temporal_sdk_core::api::errors::{PollActivityError, PollWfError};
use temporal_sdk_core_api::Worker;
use temporal_sdk_core_protos::coresdk::workflow_completion::WorkflowActivationCompletion;
use temporal_sdk_core_protos::coresdk::{ActivityHeartbeat, ActivityTaskCompletion};

use crate::client;

pyo3::create_exception!(temporal_sdk_bridge, PollShutdownError, PyException);

#[pyclass]
pub struct WorkerRef {
    worker: Arc<temporal_sdk_core::Worker>,
}

#[derive(FromPyObject)]
pub struct WorkerConfig {
    namespace: String,
    task_queue: String,
    max_cached_workflows: usize,
    max_outstanding_workflow_tasks: usize,
    max_outstanding_activities: usize,
    max_outstanding_local_activities: usize,
    max_concurrent_wft_polls: usize,
    nonsticky_to_sticky_poll_ratio: f32,
    max_concurrent_at_polls: usize,
    no_remote_activities: bool,
    sticky_queue_schedule_to_start_timeout_millis: u64,
    max_heartbeat_throttle_interval_millis: u64,
    default_heartbeat_throttle_interval_millis: u64,
}

pub fn new_worker(client: &client::ClientRef, config: WorkerConfig) -> PyResult<WorkerRef> {
    // This must be run with the Tokio context available
    let _guard = pyo3_asyncio::tokio::get_runtime().enter();
    let config: temporal_sdk_core::WorkerConfig = config.try_into()?;
    Ok(WorkerRef {
        worker: Arc::new(temporal_sdk_core::init_worker_from_upgradeable_client(
            config,
            client.retry_client.clone().into_inner(),
        )),
    })
}

#[pymethods]
impl WorkerRef {
    fn poll_workflow_activation<'p>(&self, py: Python<'p>) -> PyResult<&'p PyAny> {
        let worker = self.worker.clone();
        future_into_py(py, async move {
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
        let worker = self.worker.clone();
        future_into_py(py, async move {
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
        let worker = self.worker.clone();
        let completion = WorkflowActivationCompletion::decode(proto.as_bytes())
            .map_err(|err| PyValueError::new_err(format!("Invalid proto: {}", err)))?;
        future_into_py(py, async move {
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
        let worker = self.worker.clone();
        let completion = ActivityTaskCompletion::decode(proto.as_bytes())
            .map_err(|err| PyValueError::new_err(format!("Invalid proto: {}", err)))?;
        future_into_py(py, async move {
            worker
                .complete_activity_task(completion)
                .await
                .map_err(|err| {
                    // TODO(cretz): More error types
                    PyRuntimeError::new_err(format!("Completion failure: {}", err))
                })
        })
    }

    fn record_activity_heartbeat(&self, proto: &pyo3::types::PyBytes) -> PyResult<()> {
        let heartbeat = ActivityHeartbeat::decode(proto.as_bytes())
            .map_err(|err| PyValueError::new_err(format!("Invalid proto: {}", err)))?;
        self.worker.record_activity_heartbeat(heartbeat);
        Ok(())
    }

    fn request_workflow_eviction(&self, run_id: &str) -> PyResult<()> {
        self.worker.request_workflow_eviction(run_id);
        Ok(())
    }

    fn shutdown<'p>(&self, py: Python<'p>) -> PyResult<&'p PyAny> {
        let worker = self.worker.clone();
        future_into_py(py, async move {
            worker.shutdown().await;
            Ok(())
        })
    }

    // TODO(cretz): Python won't let self take ownership. How to do this best?
    // Make self.worker an option and move out? I don't think it's necessarily
    // clear we can use https://pyo3.rs/v0.15.1/class/protocols.html#garbage-collector-integration-1.
    // fn finalize_shutdown<'p>(self, py: Python<'p>) -> PyResult<&'p PyAny> {
}

impl TryFrom<WorkerConfig> for temporal_sdk_core::WorkerConfig {
    type Error = PyErr;

    fn try_from(conf: WorkerConfig) -> PyResult<Self> {
        temporal_sdk_core::WorkerConfigBuilder::default()
            .namespace(conf.namespace)
            .task_queue(conf.task_queue)
            .max_cached_workflows(conf.max_cached_workflows)
            .max_outstanding_workflow_tasks(conf.max_outstanding_workflow_tasks)
            .max_outstanding_activities(conf.max_outstanding_activities)
            .max_outstanding_local_activities(conf.max_outstanding_local_activities)
            .max_concurrent_wft_polls(conf.max_concurrent_wft_polls)
            .nonsticky_to_sticky_poll_ratio(conf.nonsticky_to_sticky_poll_ratio)
            .max_concurrent_at_polls(conf.max_concurrent_at_polls)
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
            .build()
            .map_err(|err| PyValueError::new_err(format!("Invalid worker config: {}", err)))
    }
}
