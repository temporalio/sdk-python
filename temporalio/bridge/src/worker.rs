use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use std::time::Duration;
use temporal_sdk_core_api::Worker;
use prost::Message;

use crate::client;

pyo3::create_exception!(temporal_sdk_bridge, PollShutdownError, pyo3::exceptions::PyException);

#[pyclass]
pub struct WorkerRef {
    worker: std::sync::Arc<temporal_sdk_core::Worker>,
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
    let config: temporal_sdk_core::WorkerConfig = config.try_into()?;
    Ok(WorkerRef {
        worker: std::sync::Arc::new(temporal_sdk_core::init_worker_from_upgradeable_client(config, client.retry_client.clone().into_inner())),
    })
}

#[pymethods]
impl WorkerRef {
    fn poll_workflow_activation<'p>(&self, py: Python<'p>) -> PyResult<&'p PyAny> {
        let worker = self.worker.clone();
        pyo3_asyncio::tokio::future_into_py(py, async move {
            let bytes = match worker.poll_workflow_activation().await {
                Ok(act) => act.encode_to_vec(),
                Err(temporal_sdk_core::api::errors::PollWfError::ShutDown) =>
                    return Err(PollShutdownError::new_err(())),
                Err(err) =>
                    return Err(PyRuntimeError::new_err(format!("Poll failure: {}", err))),
            };
            let bytes: &[u8] = &bytes;
            Ok(Python::with_gil(|py| bytes.into_py(py)))
        })
    }
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
            .sticky_queue_schedule_to_start_timeout(Duration::from_millis(conf.sticky_queue_schedule_to_start_timeout_millis))
            .max_heartbeat_throttle_interval(Duration::from_millis(conf.max_heartbeat_throttle_interval_millis))
            .default_heartbeat_throttle_interval(Duration::from_millis(conf.default_heartbeat_throttle_interval_millis))
            .build()
            .map_err(|err| PyValueError::new_err(format!("Invalid worker config: {}", err)))
    }
}
