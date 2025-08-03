import dataclasses
import warnings
from typing import cast

import pytest

import temporalio.client
import temporalio.worker
from temporalio.client import Client, ClientConfig, OutboundInterceptor
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker, WorkerConfig
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner
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
        return super().configure_client(config)

    async def connect_service_client(
        self, config: temporalio.service.ConnectConfig
    ) -> temporalio.service.ServiceClient:
        config.api_key = "replaced key"
        return await super().connect_service_client(config)


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
    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        config["task_queue"] = "combined"
        return super().configure_worker(config)


class MyWorkerPlugin(temporalio.worker.Plugin):
    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        config["task_queue"] = "replaced_queue"
        runner = config.get("workflow_runner")
        if isinstance(runner, SandboxedWorkflowRunner):
            config["workflow_runner"] = dataclasses.replace(
                runner,
                restrictions=runner.restrictions.with_passthrough_modules("my_module"),
            )
        return super().configure_worker(config)

    async def run_worker(self, worker: Worker) -> None:
        await super().run_worker(worker)


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
