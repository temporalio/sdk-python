use std::sync::Mutex;

use prost::Message;
use pyo3::{exceptions::PyRuntimeError, prelude::*, types::PyBytes};
use temporalio_common::protos::coresdk::{
    workflow_activation::WorkflowActivation, workflow_completion::WorkflowActivationCompletion,
};
use temporalio_workflow_wasm::activation::{
    ActivationComponent, ActivationInstance, LocalActivities,
};
use wasmparser::{Parser, Payload};

#[pyclass]
pub(crate) struct WasmWorkflowBundle {
    component: ActivationComponent,
    data_converter_profile: String,
}

#[pymethods]
impl WasmWorkflowBundle {
    #[new]
    fn new(py: Python<'_>, bytes: Vec<u8>) -> PyResult<Self> {
        py.detach(|| {
            let data_converter_profile = read_data_converter_profile(&bytes)?;
            let component = ActivationComponent::new(&bytes).map_err(runtime_error)?;
            Ok(Self {
                component,
                data_converter_profile,
            })
        })
    }

    fn data_converter_profile(&self) -> &str {
        &self.data_converter_profile
    }

    fn workflow_types(&self) -> Vec<String> {
        self.component.workflow_types().to_vec()
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
            self.component
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

fn read_data_converter_profile(bytes: &[u8]) -> PyResult<String> {
    let mut depth = 0;
    let mut profile = None;
    for payload in Parser::new(0).parse_all(bytes) {
        match payload.map_err(runtime_error)? {
            Payload::Version { .. } => depth += 1,
            Payload::End(_) => depth -= 1,
            Payload::CustomSection(section)
                if depth == 1 && section.name() == "temporal-python-data-converter" =>
            {
                let name = std::str::from_utf8(section.data()).map_err(runtime_error)?;
                if profile.replace(name).is_some() {
                    return Err(runtime_error("Duplicate WASM data converter profile"));
                }
            }
            _ => {}
        }
    }
    Ok(profile.unwrap_or("default").to_owned())
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
