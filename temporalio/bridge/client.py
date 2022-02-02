import os
import socket
from dataclasses import dataclass, field
from typing import Mapping, Optional, Type, TypeVar

import google.protobuf.message
import temporal_sdk_bridge

import temporalio.api.workflowservice.v1


@dataclass
class ClientTlsConfig:
    server_root_ca_cert: Optional[bytes]
    domain: Optional[str]
    client_cert: Optional[bytes]
    client_private_key: Optional[bytes]


@dataclass
class ClientRetryConfig:
    initial_interval_millis: int = 100
    randomization_factor: float = 0.2
    multiplier: float = 1.5
    max_interval_millis: int = 5000
    max_elapsed_time_millis: Optional[int] = 10000
    max_retries: int = 10


@dataclass
class ClientOptions:
    target_url: str
    client_name: str = "temporal-python"
    # TODO(cretz): Take from importlib ref https://stackoverflow.com/a/54869712
    client_version: str = "0.1.0"
    static_headers: Mapping[str, str] = field(default_factory=dict)
    identity: str = f"{os.getpid()}@{socket.gethostname()}"
    # TODO(cretz): Use proper name/version
    worker_binary_id: str = "python-sdk@0.1.0"
    tls_config: Optional[ClientTlsConfig] = None
    retry_config: Optional[ClientRetryConfig] = None


ProtoMessage = TypeVar("ProtoMessage", bound=google.protobuf.message.Message)


class Client:
    @staticmethod
    async def connect(opts: ClientOptions) -> "Client":
        return Client(await temporal_sdk_bridge.new_client(opts))

    _ref: temporal_sdk_bridge.ClientRef

    def __init__(self, ref: temporal_sdk_bridge.ClientRef):
        self._ref = ref

    async def start_workflow_execution(
        self,
        req: temporalio.api.workflowservice.v1.StartWorkflowExecutionRequest,
        *,
        retry: bool = False,
    ) -> temporalio.api.workflowservice.v1.StartWorkflowExecutionResponse:
        return await self.__rpc_call(
            "start_workflow_execution",
            req,
            temporalio.api.workflowservice.v1.StartWorkflowExecutionResponse,
            retry=retry,
        )

    async def __rpc_call(
        self,
        rpc: str,
        req: google.protobuf.message.Message,
        resp_type: Type[ProtoMessage],
        *,
        retry: bool = False,
    ) -> ProtoMessage:
        resp = resp_type()
        resp.ParseFromString(await self._ref.call(rpc, retry, req.SerializeToString()))
        return resp
