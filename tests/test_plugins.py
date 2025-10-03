import dataclasses
import uuid
import warnings
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable, Optional, cast

import pytest

import temporalio.client
import temporalio.converter
import temporalio.worker
from temporalio import workflow
from temporalio.client import Client, ClientConfig, OutboundInterceptor, WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.converter import DataConverter
from temporalio.plugin import create_plugin
from temporalio.service import ConnectConfig, ServiceClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import (
    Replayer,
    ReplayerConfig,
    Worker,
    WorkerConfig,
    WorkflowReplayResult,
)
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner
from tests.helpers import new_worker
from tests.worker.test_worker import never_run_activity


class TestClientInterceptor(temporalio.client.Interceptor):
    __test__ = False
    intercepted = False

    def intercept_client(self, next: OutboundInterceptor) -> OutboundInterceptor:
        self.intercepted = True
        return super().intercept_client(next)


class MyClientPlugin(temporalio.client.Plugin):
    def __init__(self):
        self.interceptor = TestClientInterceptor()

    def configure_client(self, config: ClientConfig) -> ClientConfig:
        config["namespace"] = "replaced_namespace"
        config["interceptors"] = list(config.get("interceptors") or []) + [
            self.interceptor
        ]
        return config

    async def connect_service_client(
        self,
        config: ConnectConfig,
        next: Callable[[ConnectConfig], Awaitable[ServiceClient]],
    ) -> ServiceClient:
        config.api_key = "replaced key"
        return await next(config)


async def test_client_plugin(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip("Client connect is only designed for local")

    plugin = MyClientPlugin()
    config = client.config()
    config["plugins"] = [plugin]
    new_client = Client(**config)
    assert new_client.namespace == "replaced_namespace"
    assert plugin.interceptor.intercepted
    assert plugin.name() == "tests.test_plugins.MyClientPlugin"

    new_client = await Client.connect(
        client.service_client.config.target_host, plugins=[MyClientPlugin()]
    )
    assert new_client.service_client.config.api_key == "replaced key"


class MyCombinedPlugin(temporalio.client.Plugin, temporalio.worker.Plugin):
    def configure_client(self, config: ClientConfig) -> ClientConfig:
        return config

    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        config["task_queue"] = "combined"
        return config

    async def connect_service_client(
        self,
        config: ConnectConfig,
        next: Callable[[ConnectConfig], Awaitable[ServiceClient]],
    ) -> ServiceClient:
        return await next(config)

    async def run_worker(
        self, worker: Worker, next: Callable[[Worker], Awaitable[None]]
    ) -> None:
        await next(worker)

    def configure_replayer(self, config: ReplayerConfig) -> ReplayerConfig:
        return config

    def run_replayer(
        self,
        replayer: Replayer,
        histories: AsyncIterator[temporalio.client.WorkflowHistory],
        next: Callable[
            [Replayer, AsyncIterator[WorkflowHistory]],
            AbstractAsyncContextManager[AsyncIterator[WorkflowReplayResult]],
        ],
    ) -> AbstractAsyncContextManager[AsyncIterator[WorkflowReplayResult]]:
        return next(replayer, histories)


class MyWorkerPlugin(temporalio.worker.Plugin):
    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        config["task_queue"] = "replaced_queue"
        runner = config.get("workflow_runner")
        if isinstance(runner, SandboxedWorkflowRunner):
            config["workflow_runner"] = dataclasses.replace(
                runner,
                restrictions=runner.restrictions.with_passthrough_modules("my_module"),
            )
        return config

    async def run_worker(
        self, worker: Worker, next: Callable[[Worker], Awaitable[None]]
    ) -> None:
        await next(worker)

    def configure_replayer(self, config: ReplayerConfig) -> ReplayerConfig:
        return config

    def run_replayer(
        self,
        replayer: Replayer,
        histories: AsyncIterator[temporalio.client.WorkflowHistory],
        next: Callable[
            [Replayer, AsyncIterator[WorkflowHistory]],
            AbstractAsyncContextManager[AsyncIterator[WorkflowReplayResult]],
        ],
    ) -> AbstractAsyncContextManager[AsyncIterator[WorkflowReplayResult]]:
        return next(replayer, histories)


async def test_worker_plugin_basic_config(client: Client) -> None:
    worker = Worker(
        client,
        task_queue="queue",
        activities=[never_run_activity],
        plugins=[MyWorkerPlugin()],
    )
    assert worker.config().get("task_queue") == "replaced_queue"

    # Test client plugin propagation to worker plugins
    new_config = client.config()
    new_config["plugins"] = [MyCombinedPlugin()]
    client = Client(**new_config)
    worker = Worker(client, task_queue="queue", activities=[never_run_activity])
    assert worker.config().get("task_queue") == "combined"

    # Test both. Client propagated plugins are called first, so the worker plugin overrides in this case
    worker = Worker(
        client,
        task_queue="queue",
        activities=[never_run_activity],
        plugins=[MyWorkerPlugin()],
    )
    assert worker.config().get("task_queue") == "replaced_queue"


async def test_worker_duplicated_plugin(client: Client) -> None:
    new_config = client.config()
    new_config["plugins"] = [MyCombinedPlugin()]
    client = Client(**new_config)

    with warnings.catch_warnings(record=True) as warning_list:
        worker = Worker(
            client,
            task_queue="queue",
            activities=[never_run_activity],
            plugins=[MyCombinedPlugin()],
        )

    assert len(warning_list) == 1
    assert "The same plugin type" in str(warning_list[0].message)


async def test_worker_sandbox_restrictions(client: Client) -> None:
    with warnings.catch_warnings(record=True) as warning_list:
        worker = Worker(
            client,
            task_queue="queue",
            activities=[never_run_activity],
            plugins=[MyWorkerPlugin()],
        )
    assert (
        "my_module"
        in cast(
            SandboxedWorkflowRunner, worker.config().get("workflow_runner")
        ).restrictions.passthrough_modules
    )


class ReplayCheckPlugin(temporalio.client.Plugin, temporalio.worker.Plugin):
    def configure_client(self, config: ClientConfig) -> ClientConfig:
        config["data_converter"] = pydantic_data_converter
        return config

    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        config["workflows"] = list(config.get("workflows") or []) + [HelloWorkflow]
        return config

    def configure_replayer(self, config: ReplayerConfig) -> ReplayerConfig:
        config["data_converter"] = pydantic_data_converter
        config["workflows"] = list(config.get("workflows") or []) + [HelloWorkflow]
        return config

    async def run_worker(
        self, worker: Worker, next: Callable[[Worker], Awaitable[None]]
    ) -> None:
        await next(worker)

    async def connect_service_client(
        self,
        config: temporalio.service.ConnectConfig,
        next: Callable[[ConnectConfig], Awaitable[ServiceClient]],
    ) -> temporalio.service.ServiceClient:
        return await next(config)

    @asynccontextmanager
    async def run_replayer(
        self,
        replayer: Replayer,
        histories: AsyncIterator[temporalio.client.WorkflowHistory],
        next: Callable[
            [Replayer, AsyncIterator[WorkflowHistory]],
            AbstractAsyncContextManager[AsyncIterator[WorkflowReplayResult]],
        ],
    ) -> AsyncIterator[AsyncIterator[WorkflowReplayResult]]:
        async with next(replayer, histories) as result:
            yield result


@workflow.defn
class HelloWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return f"Hello, {name}!"


@workflow.defn
class HelloWorkflow2:
    @workflow.run
    async def run(self, name: str) -> str:
        return f"Hello, {name}!"


async def test_replay(client: Client) -> None:
    plugin = ReplayCheckPlugin()
    new_config = client.config()
    new_config["plugins"] = [plugin]
    client = Client(**new_config)

    async with new_worker(client) as worker:
        handle = await client.start_workflow(
            HelloWorkflow.run,
            "Tim",
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()
    replayer = Replayer(workflows=[], plugins=[plugin])
    assert len(replayer.config().get("workflows") or []) == 1
    assert replayer.config().get("data_converter") == pydantic_data_converter

    await replayer.replay_workflow(await handle.fetch_history())


async def test_static_plugins(client: Client) -> None:
    plugin = create_plugin(
        "MyPlugin",
        data_converter=pydantic_data_converter,
        workflows=[HelloWorkflow2],
    )
    config = client.config()
    config["plugins"] = [plugin]
    new_client = Client(**config)

    assert new_client.data_converter == pydantic_data_converter

    # Test without plugin registered in client
    worker = Worker(
        client,
        task_queue="queue",
        activities=[never_run_activity],
        workflows=[HelloWorkflow],
        plugins=[plugin],
    )
    # On a sequence, a value is appended
    assert worker.config().get("workflows") == [HelloWorkflow, HelloWorkflow2]

    # Test with plugin registered in client
    worker = Worker(
        new_client,
        task_queue="queue",
        activities=[never_run_activity],
    )
    assert worker.config().get("workflows") == [HelloWorkflow2]

    replayer = Replayer(workflows=[HelloWorkflow], plugins=[plugin])
    assert replayer.config().get("data_converter") == pydantic_data_converter
    assert replayer.config().get("workflows") == [HelloWorkflow, HelloWorkflow2]


async def test_static_plugins_callables(client: Client) -> None:
    def converter(old: Optional[DataConverter]):
        if old != temporalio.converter.default():
            raise ValueError("Can't override non-default converter")
        return pydantic_data_converter

    plugin = create_plugin(
        "MyPlugin",
        data_converter=converter,
    )
    config = client.config()
    config["plugins"] = [plugin]
    new_client = Client(**config)

    assert new_client.data_converter == pydantic_data_converter

    with pytest.raises(ValueError):
        config["data_converter"] = pydantic_data_converter
        Client(**config)

    # On a sequence, the lambda overrides the existing values
    plugin = create_plugin(
        "MyPlugin",
        workflows=lambda workflows: [],
    )
    worker = Worker(
        client,
        task_queue="queue",
        workflows=[HelloWorkflow],
        activities=[never_run_activity],
        plugins=[plugin],
    )
    assert worker.config().get("workflows") == []
