import re
from string import Template

from google.protobuf.descriptor import (
    FileDescriptor,
    MethodDescriptor,
    ServiceDescriptor,
)

import temporalio.api.cloud.cloudservice.v1.service_pb2 as cloud_service
import temporalio.api.operatorservice.v1.service_pb2 as operator_service
import temporalio.api.testservice.v1.service_pb2 as test_service
import temporalio.api.workflowservice.v1.service_pb2 as workflow_service
import temporalio.bridge.proto.health.v1.health_pb2 as health_service


def generate_match_arm(trait_name: str, method: MethodDescriptor) -> str:
    match_template = Template("""\
          "$method_name" => { 
            rpc_call!(retry_client, call, $trait_name, $method_name)
          }""")

    return match_template.substitute(
        method_name=pascal_to_snake(method.name), trait_name=trait_name
    )


def generate_service_call(service_descriptor: ServiceDescriptor) -> str:
    print(f"generating rpc call wrapper for {service_descriptor.full_name}")

    call_template = Template("""
fn call_${service_name}<'p>(
    &self,
    py: Python<'p>,
    call: RpcCall,
  ) -> PyResult<Bound<'p, PyAny>> {
    use temporal_client::${descriptor_name};
    let mut retry_client = self.retry_client.clone();
    self.runtime.future_into_py(py, async move {
        let bytes = match call.rpc.as_str() {
$match_arms
          _ => {
            return Err(PyValueError::new_err(format!(
                "Unknown RPC call {}",
                call.rpc
            )))
          } 
        }?;
        Ok(bytes)
    })
  }""")

    sanitized_service_name: str = service_descriptor.name
    # The health service doesn't end in "Service" in the proto definition
    # this check ensures that the proto descriptor name will match the format in core
    if not sanitized_service_name.endswith("Service"):
        sanitized_service_name += "Service"

    # remove any streaming methods b/c we don't support them at the moment
    methods = [
        method
        for method in service_descriptor.methods
        if not method.client_streaming and not method.server_streaming
    ]

    match_arms = [
        generate_match_arm(sanitized_service_name, method)
        for method in sorted(methods, key=lambda m: m.name)
    ]

    return call_template.substitute(
        service_name=pascal_to_snake(sanitized_service_name),
        descriptor_name=sanitized_service_name,
        match_arms="\n".join(match_arms),
    )


def generate_client_impl(
    file_descriptors: list[FileDescriptor],
    output_file: str = "temporalio/bridge/src/client_rpc_generated.rs",
):
    print("generating bridge rpc calls")

    service_calls = [
        generate_service_call(service_descriptor)
        for file_descriptor in file_descriptors
        for service_descriptor in file_descriptor.services_by_name.values()
    ]

    impl_template = Template("""// Generated file. DO NOT EDIT

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use super::{
    client::{rpc_req, rpc_resp, ClientRef, RpcCall},
    rpc_call,
};

#[pymethods]
impl ClientRef {
$service_calls
}""")

    with open(output_file, "w") as f:
        f.write(impl_template.substitute(service_calls="\n".join(service_calls)))

    print(f"successfully generated client at {output_file}")


def pascal_to_snake(input: str) -> str:
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", input).lower()


if __name__ == "__main__":
    generate_client_impl(
        [
            workflow_service.DESCRIPTOR,
            operator_service.DESCRIPTOR,
            cloud_service.DESCRIPTOR,
            test_service.DESCRIPTOR,
            health_service.DESCRIPTOR,
        ]
    )
