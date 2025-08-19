import dataclasses
import uuid
import warnings
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import AsyncIterator, cast

import pytest

import temporalio.client
import temporalio.plugin
import temporalio.worker
from temporalio import workflow
from temporalio.client import Client, ClientConfig, LowLevelPlugin, OutboundInterceptor
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.plugin import PluginConfig
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


class MyClientPlugin(temporalio.client.LowLevelPlugin):
    def __init__(self):
        self.interceptor = TestClientInterceptor()

    def init_client_plugin(self, next: LowLevelPlugin) -> None:
        self.next_client_plugin = next

    def configure_client(self, config: ClientConfig) -> ClientConfig:
        config["namespace"] = "replaced_namespace"
        config["interceptors"] = list(config.get("interceptors") or []) + [
            self.interceptor
        ]
        return self.next_client_plugin.configure_client(config)

    async def connect_service_client(
        self, config: temporalio.service.ConnectConfig
    ) -> temporalio.service.ServiceClient:
        config.api_key = "replaced key"
        return await self.next_client_plugin.connect_service_client(config)


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


class MyCombinedPlugin(
    temporalio.client.LowLevelPlugin, temporalio.worker.LowLevelPlugin
):
    def init_worker_plugin(self, next: temporalio.worker.LowLevelPlugin) -> None:
        self.next_worker_plugin = next

    def init_client_plugin(self, next: temporalio.client.LowLevelPlugin) -> None:
        self.next_client_plugin = next

    def configure_client(self, config: ClientConfig) -> ClientConfig:
        return self.next_client_plugin.configure_client(config)

    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        config["task_queue"] = "combined"
        return self.next_worker_plugin.configure_worker(config)

    async def connect_service_client(
        self, config: temporalio.service.ConnectConfig
    ) -> temporalio.service.ServiceClient:
        return await self.next_client_plugin.connect_service_client(config)

    async def run_worker(self, worker: Worker) -> None:
        await self.next_worker_plugin.run_worker(worker)

    def configure_replayer(self, config: ReplayerConfig) -> ReplayerConfig:
        return self.next_worker_plugin.configure_replayer(config)

    def run_replayer(
        self,
        replayer: Replayer,
        histories: AsyncIterator[temporalio.client.WorkflowHistory],
    ) -> AbstractAsyncContextManager[AsyncIterator[WorkflowReplayResult]]:
        return self.next_worker_plugin.run_replayer(replayer, histories)


class MyWorkerPlugin(temporalio.worker.LowLevelPlugin):
    def init_worker_plugin(self, next: temporalio.worker.LowLevelPlugin) -> None:
        self.next_worker_plugin = next

    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        config["task_queue"] = "replaced_queue"
        runner = config.get("workflow_runner")
        if isinstance(runner, SandboxedWorkflowRunner):
            config["workflow_runner"] = dataclasses.replace(
                runner,
                restrictions=runner.restrictions.with_passthrough_modules("my_module"),
            )
        return self.next_worker_plugin.configure_worker(config)

    async def run_worker(self, worker: Worker) -> None:
        await self.next_worker_plugin.run_worker(worker)

    def configure_replayer(self, config: ReplayerConfig) -> ReplayerConfig:
        return self.next_worker_plugin.configure_replayer(config)

    def run_replayer(
        self,
        replayer: Replayer,
        histories: AsyncIterator[temporalio.client.WorkflowHistory],
    ) -> AbstractAsyncContextManager[AsyncIterator[WorkflowReplayResult]]:
        return self.next_worker_plugin.run_replayer(replayer, histories)


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


class ReplayCheckPlugin(
    temporalio.client.LowLevelPlugin, temporalio.worker.LowLevelPlugin
):
    def init_worker_plugin(self, next: temporalio.worker.LowLevelPlugin) -> None:
        self.next_worker_plugin = next

    def init_client_plugin(self, next: temporalio.client.LowLevelPlugin) -> None:
        self.next_client_plugin = next

    def configure_client(self, config: ClientConfig) -> ClientConfig:
        config["data_converter"] = pydantic_data_converter
        return self.next_client_plugin.configure_client(config)

    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        config["workflows"] = list(config.get("workflows") or []) + [HelloWorkflow]
        return self.next_worker_plugin.configure_worker(config)

    def configure_replayer(self, config: ReplayerConfig) -> ReplayerConfig:
        config["data_converter"] = pydantic_data_converter
        config["workflows"] = list(config.get("workflows") or []) + [HelloWorkflow]
        return self.next_worker_plugin.configure_replayer(config)

    async def run_worker(self, worker: Worker) -> None:
        await self.next_worker_plugin.run_worker(worker)

    async def connect_service_client(
        self, config: temporalio.service.ConnectConfig
    ) -> temporalio.service.ServiceClient:
        return await self.next_client_plugin.connect_service_client(config)

    @asynccontextmanager
    async def run_replayer(
        self,
        replayer: Replayer,
        histories: AsyncIterator[temporalio.client.WorkflowHistory],
    ) -> AsyncIterator[AsyncIterator[WorkflowReplayResult]]:
        async with self.next_worker_plugin.run_replayer(replayer, histories) as result:
            yield result


@workflow.defn
class HelloWorkflow:
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


class SimplePlugin(temporalio.plugin.Plugin):
    @asynccontextmanager
    async def run_context(self) -> AsyncIterator[None]:
        yield

    def configuration(self) -> PluginConfig:
        return PluginConfig(
            data_converter=pydantic_data_converter,
            workflows=[HelloWorkflow],
        )


async def test_simple_plugin(client: Client) -> None:
    plugin = SimplePlugin()
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
