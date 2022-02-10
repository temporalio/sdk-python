from dataclasses import dataclass, field
from enum import IntEnum
from typing import Mapping, Optional, Type, TypeVar

import google.protobuf.message
import temporal_sdk_bridge
from temporal_sdk_bridge import RPCError


@dataclass
class ClientTlsConfig:
    server_root_ca_cert: Optional[bytes]
    domain: Optional[str]
    client_cert: Optional[bytes]
    client_private_key: Optional[bytes]


@dataclass
class ClientRetryConfig:
    initial_interval_millis: int
    randomization_factor: float
    multiplier: float
    max_interval_millis: int
    max_elapsed_time_millis: Optional[int]
    max_retries: int


@dataclass
class ClientOptions:
    target_url: str
    static_headers: Mapping[str, str]
    identity: str
    worker_binary_id: str
    tls_config: Optional[ClientTlsConfig]
    retry_config: Optional[ClientRetryConfig]
    client_name: str = "temporal-python"
    # TODO(cretz): Take from importlib ref https://stackoverflow.com/a/54869712
    client_version: str = "0.1.0"


ProtoMessage = TypeVar("ProtoMessage", bound=google.protobuf.message.Message)


class Client:
    @staticmethod
    async def connect(opts: ClientOptions) -> "Client":
        return Client(await temporal_sdk_bridge.connect_client(opts))

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
        resp = resp_type()
        resp.ParseFromString(await self._ref.call(rpc, retry, req.SerializeToString()))
        return resp


def load_worker_binary_id() -> str:
    # TODO(cretz): See if there's a reasonable way to build up a hash of the
    # current runtime including user code here without walking disk too much.
    # We can use importlib and module __cache__ pyc paths and such, but it's not
    # yet clear what we can obtain from importlib without using disk.
    return "python-sdk@0.1.0"
