from __future__ import annotations

import asyncio
import os
import uuid
from abc import ABC, abstractmethod
from typing import Optional

from temporalio.client import Client, TLSConfig
from tests.helpers.golang import start_external_go_process


class ExternalServer(ABC):
    @property
    @abstractmethod
    def host_port(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def namespace(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def supports_custom_search_attributes(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def close(self):
        raise NotImplementedError

    async def new_client(self) -> Client:
        return await Client.connect(self.host_port, namespace=self.namespace)

    async def new_tls_client(self) -> Optional[Client]:
        return None


class LocalhostDefaultServer(ExternalServer):
    @property
    def host_port(self):
        return "localhost:7233"

    @property
    def namespace(self):
        return "default"

    @property
    def supports_custom_search_attributes(self) -> bool:
        return False

    async def close(self):
        pass


class ExternalGolangServer(ExternalServer):
    @staticmethod
    async def start() -> ExternalGolangServer:
        namespace = f"test-namespace-{uuid.uuid4()}"
        # TODO(cretz): Make this configurable?
        port = "9233"
        process = await start_external_go_process(
            os.path.join(os.path.dirname(__file__), "golangserver"),
            "golangserver",
            port,
            namespace,
        )
        server = ExternalGolangServer(
            host_port=f"localhost:{port}",
            tls_host_port=f"localhost:{int(port)+1000}",
            namespace=namespace,
            process=process,
        )
        # Try to get the client multiple times to check whether server is online
        last_err: RuntimeError
        for _ in range(30):
            try:
                await server.new_client()
                return server
            except RuntimeError as err:
                last_err = err
                await asyncio.sleep(1)
        raise last_err

    def __init__(
        self,
        *,
        host_port: str,
        tls_host_port: str,
        namespace: str,
        process: asyncio.subprocess.Process,
    ) -> None:
        super().__init__()
        self._host_port = host_port
        self._tls_host_port = tls_host_port
        self._namespace = namespace
        self._process = process

    @property
    def host_port(self) -> str:
        return self._host_port

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def supports_custom_search_attributes(self) -> bool:
        return True

    async def close(self):
        self._process.terminate()
        await self._process.wait()

    async def new_tls_client(self) -> Optional[Client]:
        # Read certs
        certs_dir = os.path.join(os.path.dirname(__file__), "golangserver", "certs")
        with open(os.path.join(certs_dir, "server-ca-cert.pem"), "rb") as f:
            server_root_ca_cert = f.read()
        with open(os.path.join(certs_dir, "client-cert.pem"), "rb") as f:
            client_cert = f.read()
        with open(os.path.join(certs_dir, "client-key.pem"), "rb") as f:
            client_private_key = f.read()
        return await Client.connect(
            target_host=self._tls_host_port,
            namespace=self._namespace,
            tls=TLSConfig(
                server_root_ca_cert=server_root_ca_cert,
                client_cert=client_cert,
                client_private_key=client_private_key,
            ),
        )
