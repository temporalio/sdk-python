"""RPC client using SDK Core."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Type, TypeVar

import google.protobuf.message

import temporalio.bridge.temporal_sdk_bridge
from temporalio.bridge.temporal_sdk_bridge import RPCError


@dataclass
class ClientTlsConfig:
    """Python representation of the Rust struct for configuring TLS."""

    server_root_ca_cert: Optional[bytes]
    domain: Optional[str]
    client_cert: Optional[bytes]
    client_private_key: Optional[bytes]


@dataclass
class ClientRetryConfig:
    """Python representation of the Rust struct for configuring retry."""

    initial_interval_millis: int
    randomization_factor: float
    multiplier: float
    max_interval_millis: int
    max_elapsed_time_millis: Optional[int]
    max_retries: int


@dataclass
class ClientConfig:
    """Python representation of the Rust struct for configuring the client."""

    target_url: str
    static_headers: Mapping[str, str]
    identity: str
    tls_config: Optional[ClientTlsConfig]
    retry_config: Optional[ClientRetryConfig]
    client_name: str
    client_version: str


ProtoMessage = TypeVar("ProtoMessage", bound=google.protobuf.message.Message)


class Client:
    """RPC client using SDK Core."""

    @staticmethod
    async def connect(config: ClientConfig) -> Client:
        """Establish connection with server."""
        return Client(
            await temporalio.bridge.temporal_sdk_bridge.connect_client(config)
        )

    def __init__(self, ref: temporalio.bridge.temporal_sdk_bridge.ClientRef):
        """Initialize client with underlying SDK Core reference."""
        self._ref = ref

    async def rpc_call(
        self,
        rpc: str,
        req: google.protobuf.message.Message,
        resp_type: Type[ProtoMessage],
        *,
        retry: bool = False,
    ) -> ProtoMessage:
        """Make RPC call using SDK Core."""
        resp = resp_type()
        resp.ParseFromString(await self._ref.call(rpc, retry, req.SerializeToString()))
        return resp
