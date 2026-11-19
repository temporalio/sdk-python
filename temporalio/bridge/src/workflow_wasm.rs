use std::sync::Mutex;

use prost::Message;
use pyo3::{exceptions::PyRuntimeError, prelude::*, types::PyBytes};
use temporalio_common::protos::coresdk::{
    workflow_activation::WorkflowActivation, workflow_completion::WorkflowActivationCompletion,
};
use temporalio_workflow_wasm::activation::{
    ActivationComponent, ActivationInstance, LocalActivities,
};

#[pyclass]
pub(crate) struct WasmWorkflowBundle(ActivationComponent);

#[pymethods]
impl WasmWorkflowBundle {
    #[new]
    fn new(py: Python<'_>, bytes: Vec<u8>) -> PyResult<Self> {
        py.detach(|| {
            ActivationComponent::new(&bytes)
                .map(Self)
                .map_err(runtime_error)
        })
    }

    fn workflow_types(&self) -> Vec<String> {
        self.0.workflow_types().to_vec()
    }

    fn instantiate(
        &self,
        py: Python<'_>,
        namespace: String,
        task_queue: String,
        enabled_logic_flags: Vec<u32>,
        local_activities: Option<(Vec<String>, bool)>,
        disable_eager_activity_execution: bool,
    ) -> PyResult<WasmWorkflowInstance> {
        let registry = local_activities.map(|(names, has_dynamic_handler)| LocalActivities {
            names,
            has_dynamic_handler,
        });
        py.detach(|| {
            self.0
                .instantiate(
                    &namespace,
                    &task_queue,
                    &enabled_logic_flags,
                    registry.as_ref(),
                    disable_eager_activity_execution,
                )
                .map(|instance| WasmWorkflowInstance(Mutex::new(instance)))
                .map_err(runtime_error)
        })
    }
}

#[pyclass]
pub(crate) struct WasmWorkflowInstance(Mutex<ActivationInstance>);

#[pymethods]
impl WasmWorkflowInstance {
    fn activate<'py>(&self, py: Python<'py>, bytes: Vec<u8>) -> PyResult<Bound<'py, PyBytes>> {
        let completion = py.detach(|| {
            let act = WorkflowActivation::decode(bytes.as_slice()).map_err(runtime_error)?;
            let output = self
                .0
                .lock()
                .map_err(runtime_error)?
                .activate(&bytes)
                .map_err(runtime_error)?;
            let completion =
                WorkflowActivationCompletion::decode(output.as_slice()).map_err(runtime_error)?;
            if completion.run_id != act.run_id || completion.status.is_none() {
                return Err(PyRuntimeError::new_err(
                    "Invalid WASM workflow activation completion",
                ));
            }
            Ok(output)
        })?;
        Ok(PyBytes::new(py, &completion))
    }
}

fn runtime_error(err: impl std::fmt::Display) -> PyErr {
    PyRuntimeError::new_err(format!("{err:#}"))
}
