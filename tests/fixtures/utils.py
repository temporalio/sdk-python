import asyncio
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Optional

import pytest_asyncio

import temporalio.client


class Server(ABC):
    @property
    @abstractmethod
    def host_port(self):
        raise NotImplementedError

    @property
    def target_url(self):
        return f"http://{self.host_port}"

    @property
    @abstractmethod
    def namespace(self):
        raise NotImplementedError

    @abstractmethod
    def close(self):
        raise NotImplementedError

    async def new_client(self) -> temporalio.client.Client:
        return await temporalio.client.Client.connect(
            self.target_url, namespace=self.namespace
        )


class LocalhostDefaultServer(Server):
    @property
    def host_port(self):
        return "localhost:7233"

    @property
    def namespace(self):
        return "default"

    def close(self):
        pass


@pytest_asyncio.fixture(scope="session")
async def server() -> Server:
    # TODO(cretz): More options such as temporalite and our test server
    return LocalhostDefaultServer()


@pytest_asyncio.fixture
async def client(server: Server) -> temporalio.client.Client:
    return await server.new_client()


@dataclass
class KitchenSinkWorkflowParams:
    result: Optional[Any] = None
    error_with: Optional[str] = None


class Worker(ABC):
    """Worker guaranteed to have a "kitchen_sink" workflow."""

    @property
    @abstractmethod
    def task_queue(self) -> str:
        raise NotImplementedError

    @abstractmethod
    async def close(self):
        raise NotImplementedError


class RemoteGolangWorker(Worker):
    @staticmethod
    async def start(host_port: str, namespace: str) -> "RemoteGolangWorker":
        task_queue = uuid.uuid4()
        process = await asyncio.create_subprocess_exec(
            "go",
            "run",
            ".",
            host_port,
            namespace,
            str(task_queue),
            cwd=os.path.abspath(
                os.path.join(os.path.dirname(__file__), "golangworker")
            ),
        )
        return RemoteGolangWorker(str(task_queue), process)

    def __init__(self, task_queue: str, process: asyncio.subprocess.Process) -> None:
        super().__init__()
        self._task_queue = task_queue
        self._process = process

    @property
    def task_queue(self) -> str:
        return self._task_queue

    async def close(self):
        self._process.terminate()
        await self._process.wait()


@pytest_asyncio.fixture(scope="session")
async def worker(server: Server) -> AsyncGenerator[Worker, None]:
    worker = await RemoteGolangWorker.start(server.host_port, server.namespace)
    yield worker
    await worker.close()
