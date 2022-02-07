import os
import socket
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Mapping, Optional, Type, TypeVar

import google.protobuf.message
import grpc
import temporal_sdk_bridge


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


class RPCStatusCode(IntEnum):
    OK = grpc.StatusCode.OK.value[0]
    CANCELLED = grpc.StatusCode.CANCELLED.value[0]
    UNKNOWN = grpc.StatusCode.UNKNOWN.value[0]
    INVALID_ARGUMENT = grpc.StatusCode.INVALID_ARGUMENT.value[0]
    DEADLINE_EXCEEDED = grpc.StatusCode.DEADLINE_EXCEEDED.value[0]
    NOT_FOUND = grpc.StatusCode.NOT_FOUND.value[0]
    ALREADY_EXISTS = grpc.StatusCode.ALREADY_EXISTS.value[0]
    PERMISSION_DENIED = grpc.StatusCode.PERMISSION_DENIED.value[0]
    RESOURCE_EXHAUSTED = grpc.StatusCode.RESOURCE_EXHAUSTED.value[0]
    FAILED_PRECONDITION = grpc.StatusCode.FAILED_PRECONDITION.value[0]
    ABORTED = grpc.StatusCode.ABORTED.value[0]
    OUT_OF_RANGE = grpc.StatusCode.OUT_OF_RANGE.value[0]
    UNIMPLEMENTED = grpc.StatusCode.UNIMPLEMENTED.value[0]
    INTERNAL = grpc.StatusCode.INTERNAL.value[0]
    UNAVAILABLE = grpc.StatusCode.UNAVAILABLE.value[0]
    DATA_LOSS = grpc.StatusCode.DATA_LOSS.value[0]
    UNAUTHENTICATED = grpc.StatusCode.UNAUTHENTICATED.value[0]


class RPCError(RuntimeError):
    def __init__(self, raw: temporal_sdk_bridge.RPCError) -> None:
        status, message, details = raw.args
        super().__init__(message)
        self._status = RPCStatusCode(status)
        self._details = details

    @property
    def status(self) -> RPCStatusCode:
        return self._status

    @property
    def details(self) -> bytes:
        return self._details


class Client:
    @staticmethod
    async def connect(opts: ClientOptions) -> "Client":
        return Client(await temporal_sdk_bridge.new_client(opts))

    _ref: temporal_sdk_bridge.ClientRef

    def __init__(self, ref: temporal_sdk_bridge.ClientRef):
        self._ref = ref

    async def rpc_call(
        self,
        rpc: str,
        req: google.protobuf.message.Message,
        resp_type: Type[ProtoMessage],
        *,
        retry: bool = False,
    ) -> ProtoMessage:
        try:
            resp = resp_type()
            resp.ParseFromString(
                await self._ref.call(rpc, retry, req.SerializeToString())
            )
            return resp
        except temporal_sdk_bridge.RPCError as err:
            # Intentionally swallow the bridge error after conversion
            raise RPCError(err) from None
