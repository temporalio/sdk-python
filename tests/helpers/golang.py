from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import uuid

from temporalio.client import Client, TLSConfig

logger = logging.getLogger(__name__)

# TODO(cretz): Remove this when Temporalite supports search attributes and TLS
class ExternalGolangServer:
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
        self.host_port = host_port
        self.tls_host_port = tls_host_port
        self.namespace = namespace
        self.process = process

    async def close(self):
        self.process.terminate()
        await self.process.wait()

    async def new_client(self) -> Client:
        return await Client.connect(self.host_port, namespace=self.namespace)

    async def new_tls_client(self) -> Client:
        # Read certs
        certs_dir = os.path.join(os.path.dirname(__file__), "golangserver", "certs")
        with open(os.path.join(certs_dir, "server-ca-cert.pem"), "rb") as f:
            server_root_ca_cert = f.read()
        with open(os.path.join(certs_dir, "client-cert.pem"), "rb") as f:
            client_cert = f.read()
        with open(os.path.join(certs_dir, "client-key.pem"), "rb") as f:
            client_private_key = f.read()
        return await Client.connect(
            target_host=self.tls_host_port,
            namespace=self.namespace,
            tls=TLSConfig(
                server_root_ca_cert=server_root_ca_cert,
                client_cert=client_cert,
                client_private_key=client_private_key,
            ),
        )


async def start_external_go_process(
    source_dir: str, exe_name: str, *args: str
) -> asyncio.subprocess.Process:
    # First, build the executable. We accept the performance issues of building
    # this each run.
    logger.info("Building %s", exe_name)
    subprocess.run(["go", "build", "-o", exe_name, "."], cwd=source_dir, check=True)
    logger.info("Starting %s", exe_name)
    return await asyncio.create_subprocess_exec(
        os.path.join(source_dir, exe_name), *args
    )
