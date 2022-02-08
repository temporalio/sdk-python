import asyncio
import logging
import os
import signal
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Optional

import psutil
import pytest_asyncio

import temporalio.api.workflowservice.v1
import temporalio.client

logger = logging.getLogger(__name__)


class Server(ABC):
    @property
    @abstractmethod
    def host_port(self) -> str:
        raise NotImplementedError

    @property
    def target_url(self) -> str:
        return f"http://{self.host_port}"

    @property
    @abstractmethod
    def namespace(self) -> str:
        raise NotImplementedError

    @abstractmethod
    async def close(self):
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

    async def close(self):
        pass


class ExternalGolangServer(Server):
    @staticmethod
    async def start() -> "ExternalGolangServer":
        namespace = f"test-namespace-{uuid.uuid4()}"
        # TODO(cretz): Make this configurable?
        port = "9233"
        process = await asyncio.create_subprocess_exec(
            "go",
            "run",
            ".",
            port,
            namespace,
            cwd=os.path.abspath(
                os.path.join(os.path.dirname(__file__), "golangserver")
            ),
        )
        server = ExternalGolangServer(f"localhost:{port}", namespace, process)
        # Try to get the client multiple times to check whether server is online
        last_err: RuntimeError
        for _ in range(10):
            try:
                await server.new_client()
                return server
            except RuntimeError as err:
                last_err = err
                await asyncio.sleep(0.1)
        raise last_err

    def __init__(
        self, host_port: str, namespace: str, process: asyncio.subprocess.Process
    ) -> None:
        super().__init__()
        self._host_port = host_port
        self._namespace = namespace
        self._process = process

    @property
    def host_port(self) -> str:
        return self._host_port

    @property
    def namespace(self) -> str:
        return self._namespace

    async def close(self):
        kill_proc_tree(self._process.pid)
        await self._process.wait()


@pytest_asyncio.fixture(scope="session")
async def server() -> AsyncGenerator[Server, None]:
    # TODO(cretz): More options such as our test server
    server = await ExternalGolangServer.start()
    yield server
    await server.close()


@pytest_asyncio.fixture
async def client(server: Server) -> temporalio.client.Client:
    return await server.new_client()


@dataclass
class KitchenSinkWorkflowParams:
    result: Optional[Any] = None
    error_with: Optional[str] = None
    error_details: Optional[Any] = None
    continue_as_new_count: Optional[int] = None
    result_as_string_signal_arg: Optional[str] = None
    result_as_run_id: Optional[bool] = None
    sleep_ms: Optional[int] = None


class Worker(ABC):
    """Worker guaranteed to have a "kitchen_sink" workflow."""

    @property
    @abstractmethod
    def task_queue(self) -> str:
        raise NotImplementedError

    @abstractmethod
    async def close(self):
        raise NotImplementedError


class ExternalGolangWorker(Worker):
    @staticmethod
    async def start(host_port: str, namespace: str) -> "ExternalGolangWorker":
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
        return ExternalGolangWorker(str(task_queue), process)

    def __init__(self, task_queue: str, process: asyncio.subprocess.Process) -> None:
        super().__init__()
        self._task_queue = task_queue
        self._process = process

    @property
    def task_queue(self) -> str:
        return self._task_queue

    async def close(self):
        kill_proc_tree(self._process.pid)
        await self._process.wait()


@pytest_asyncio.fixture(scope="session")
async def worker(server: Server) -> AsyncGenerator[Worker, None]:
    worker = await ExternalGolangWorker.start(server.host_port, server.namespace)
    yield worker
    await worker.close()


# See https://psutil.readthedocs.io/en/latest/#kill-process-tree
def kill_proc_tree(
    pid, sig=signal.SIGTERM, include_parent=True, timeout=None, on_terminate=None
):
    """Kill a process tree (including grandchildren) with signal
    "sig" and return a (gone, still_alive) tuple.
    "on_terminate", if specified, is a callback function which is
    called as soon as a child terminates.
    """
    assert pid != os.getpid(), "won't kill myself"
    parent = psutil.Process(pid)
    children = parent.children(recursive=True)
    if include_parent:
        children.append(parent)
    for p in children:
        try:
            p.send_signal(sig)
        except psutil.NoSuchProcess:
            pass
    gone, alive = psutil.wait_procs(children, timeout=timeout, callback=on_terminate)
    return (gone, alive)
