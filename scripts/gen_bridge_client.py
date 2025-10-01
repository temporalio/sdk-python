import re
from functools import partial
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


def generate_python_services(
    file_descriptors: list[FileDescriptor],
    output_file: str = "temporalio/bridge/generated/services_generated.py",
):
    print("generating python services")

    services_template = Template("""# Generated file. DO NOT EDIT

from __future__ import annotations

from datetime import timedelta
from typing import Mapping, Optional, Union, TYPE_CHECKING
import google.protobuf.empty_pb2

$service_imports


if TYPE_CHECKING:
    from temporalio.service import ServiceClient

$service_defns
""")

    def service_name(s):
        return f"import {sanitize_proto_name(s.full_name)[:-len(s.name)-1]}"

    service_imports = [
        service_name(service_descriptor)
        for file_descriptor in file_descriptors
        for service_descriptor in file_descriptor.services_by_name.values()
    ]

    service_defns = [
        generate_python_service(service_descriptor)
        for file_descriptor in file_descriptors
        for service_descriptor in file_descriptor.services_by_name.values()
    ]

    with open(output_file, "w") as f:
        f.write(
            services_template.substitute(
                service_imports="\n".join(service_imports),
                service_defns="\n".join(service_defns),
            )
        )


def generate_python_service(service_descriptor: ServiceDescriptor) -> str:
    service_template = Template("""
class $service_name:
    def __init__(self, client: ServiceClient):
        self.client = client
        self.service = "$rpc_service_name"
$method_calls
""")

    sanitized_service_name: str = service_descriptor.name
    # The health service doesn't end in "Service" in the proto definition
    # this check ensures that the proto descriptor name will match the format in core
    if not sanitized_service_name.endswith("Service"):
        sanitized_service_name += "Service"

    # remove "Service" and lowercase
    rpc_name = sanitized_service_name[:-7].lower()

    # remove any streaming methods b/c we don't support them at the moment
    methods = [
        method
        for method in service_descriptor.methods
        if not method.client_streaming and not method.server_streaming
    ]

    method_calls = [
        generate_python_method_call(method)
        for method in sorted(methods, key=lambda m: m.name)
    ]

    return service_template.substitute(
        service_name=sanitized_service_name,
        rpc_service_name=pascal_to_snake(rpc_name),
        method_calls="\n".join(method_calls),
    )


def generate_python_method_call(method_descriptor: MethodDescriptor) -> str:
    method_template = Template("""
    async def $method_name(
        self,
        req: $request_type,
        retry: bool = False,
        metadata: Mapping[str, Union[str, bytes]] = {},
        timeout: Optional[timedelta] = None,
    ) -> $response_type:
        return await self.client._rpc_call(
            rpc="$method_name",
            req=req,
            service=self.service,
            resp_type=$response_type,
            retry=retry,
            metadata=metadata,
            timeout=timeout,
        )
""")

    return method_template.substitute(
        method_name=pascal_to_snake(method_descriptor.name),
        request_type=sanitize_proto_name(method_descriptor.input_type.full_name),
        response_type=sanitize_proto_name(method_descriptor.output_type.full_name),
    )


def generate_rust_client_impl(
    file_descriptors: list[FileDescriptor],
    output_file: str = "temporalio/bridge/src/client_rpc_generated.rs",
):
    print("generating bridge rpc calls")

    service_calls = [
        generate_rust_service_call(service_descriptor)
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


def generate_rust_service_call(service_descriptor: ServiceDescriptor) -> str:
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
        generate_rust_match_arm(sanitized_service_name, method)
        for method in sorted(methods, key=lambda m: m.name)
    ]

    return call_template.substitute(
        service_name=pascal_to_snake(sanitized_service_name),
        descriptor_name=sanitized_service_name,
        match_arms="\n".join(match_arms),
    )


def generate_rust_match_arm(trait_name: str, method: MethodDescriptor) -> str:
    match_template = Template("""\
          "$method_name" => { 
            rpc_call!(retry_client, call, $trait_name, $method_name)
          }""")

    return match_template.substitute(
        method_name=pascal_to_snake(method.name), trait_name=trait_name
    )


def pascal_to_snake(input: str) -> str:
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", input).lower()


sanitize_import_fixes = [
    partial(re.compile(r"temporal\.api\.").sub, r"temporalio.api."),
    partial(
        re.compile(r"temporal\.grpc.health\.").sub, r"temporalio.bridge.proto.health."
    ),
    partial(
        re.compile(r"google\.protobuf\.Empty").sub, r"google.protobuf.empty_pb2.Empty"
    ),
]


def sanitize_proto_name(input: str) -> str:
    content = input
    for fix in sanitize_import_fixes:
        content = fix(content)
    return content


if __name__ == "__main__":
    generate_rust_client_impl(
        [
            workflow_service.DESCRIPTOR,
            operator_service.DESCRIPTOR,
            cloud_service.DESCRIPTOR,
            test_service.DESCRIPTOR,
            health_service.DESCRIPTOR,
        ]
    )

    generate_python_services(
        [
            workflow_service.DESCRIPTOR,
            operator_service.DESCRIPTOR,
            cloud_service.DESCRIPTOR,
            test_service.DESCRIPTOR,
            health_service.DESCRIPTOR,
        ]
    )
