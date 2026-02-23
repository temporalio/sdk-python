import dataclasses
import uuid
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import cast

import pytest

import temporalio.bridge.temporal_sdk_bridge
import temporalio.client
import temporalio.converter
import temporalio.worker
from temporalio import workflow
from temporalio.client import Client, ClientConfig, OutboundInterceptor, WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.converter import DataConverter
from temporalio.plugin import SimplePlugin
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
        config.tls = False
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
        config["task_queue"] = "combined" + str(uuid.uuid4())
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
        config["task_queue"] = "replaced_queue" + str(uuid.uuid4())
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
        task_queue="queue" + str(uuid.uuid4()),
        activities=[never_run_activity],
        plugins=[MyWorkerPlugin()],
    )
    task_queue = worker.config(active_config=True).get("task_queue")
    assert task_queue is not None and task_queue.startswith("replaced_queue")

    # Test client plugin propagation to worker plugins
    new_config = client.config()
    new_config["plugins"] = [MyCombinedPlugin()]
    client = Client(**new_config)
    worker = Worker(
        client, task_queue="queue" + str(uuid.uuid4()), activities=[never_run_activity]
    )
    task_queue = worker.config(active_config=True).get("task_queue")
    assert task_queue is not None and task_queue.startswith("combined")

    # Test both. Client propagated plugins are called first, so the worker plugin overrides in this case
    worker = Worker(
        client,
        task_queue="queue" + str(uuid.uuid4()),
        activities=[never_run_activity],
        plugins=[MyWorkerPlugin()],
    )
    task_queue = worker.config(active_config=True).get("task_queue")
    assert task_queue is not None and task_queue.startswith("replaced_queue")


async def test_worker_duplicated_plugin(client: Client) -> None:
    new_config = client.config()
    new_config["plugins"] = [MyCombinedPlugin()]
    client = Client(**new_config)

    with warnings.catch_warnings(record=True) as warning_list:
        Worker(
            client,
            task_queue="queue" + str(uuid.uuid4()),
            activities=[never_run_activity],
            plugins=[MyCombinedPlugin()],
        )

    assert len(warning_list) == 1
    assert "The same plugin type" in str(warning_list[0].message)


async def test_worker_sandbox_restrictions(client: Client) -> None:
    with warnings.catch_warnings(record=True):
        worker = Worker(
            client,
            task_queue="queue" + str(uuid.uuid4()),
            activities=[never_run_activity],
            plugins=[MyWorkerPlugin()],
        )
    assert (
        "my_module"
        in cast(
            SandboxedWorkflowRunner,
            worker.config(active_config=True).get("workflow_runner"),
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
    assert len(replayer.config(active_config=True).get("workflows") or []) == 1
    assert (
        replayer.config(active_config=True).get("data_converter")
        == pydantic_data_converter
    )

    await replayer.replay_workflow(await handle.fetch_history())


async def test_simple_plugins(client: Client) -> None:
    plugin = SimplePlugin(
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
        task_queue="queue" + str(uuid.uuid4()),
        activities=[never_run_activity],
        workflows=[HelloWorkflow],
        plugins=[plugin],
    )
    # On a sequence, a value is appended
    assert worker.config(active_config=True).get("workflows") == [
        HelloWorkflow,
        HelloWorkflow2,
    ]

    # Test with plugin registered in client
    worker = Worker(
        new_client,
        task_queue="queue" + str(uuid.uuid4()),
        activities=[never_run_activity],
    )
    assert worker.config(active_config=True).get("workflows") == [HelloWorkflow2]

    replayer = Replayer(workflows=[HelloWorkflow], plugins=[plugin])
    assert (
        replayer.config(active_config=True).get("data_converter")
        == pydantic_data_converter
    )
    assert replayer.config(active_config=True).get("workflows") == [
        HelloWorkflow,
        HelloWorkflow2,
    ]


async def test_simple_plugins_callables(client: Client) -> None:
    def converter(old: DataConverter | None):
        if old != temporalio.converter.default():
            raise ValueError("Can't override non-default converter")
        return pydantic_data_converter

    plugin = SimplePlugin(
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
    plugin = SimplePlugin(
        "MyPlugin",
        workflows=lambda workflows: [],
    )
    worker = Worker(
        client,
        task_queue="queue" + str(uuid.uuid4()) + str(uuid.uuid4()),
        workflows=[HelloWorkflow],
        activities=[never_run_activity],
        plugins=[plugin],
    )
    assert worker.config(active_config=True).get("workflows") == []


class MediumPlugin(SimplePlugin):
    def __init__(self):
        super().__init__("MediumPlugin", data_converter=pydantic_data_converter)

    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        config = super().configure_worker(config)
        config["task_queue"] = "override" + str(uuid.uuid4())
        return config


async def test_medium_plugin(client: Client) -> None:
    plugin = MediumPlugin()
    worker = Worker(
        client,
        task_queue="queue" + str(uuid.uuid4()),
        plugins=[plugin],
        workflows=[HelloWorkflow],
    )
    task_queue = worker.config(active_config=True).get("task_queue")
    assert task_queue is not None and task_queue.startswith("override")


class CombinedClientWorkerInterceptor(
    temporalio.client.Interceptor, temporalio.worker.Interceptor
):
    """Test interceptor that can be used as both client and worker interceptor with execution counting."""

    def __init__(self):
        super().__init__()
        self.client_intercepted = False
        self.worker_intercepted = False
        self.call_count = {"execute_workflow": 0}

    def intercept_client(
        self, next: temporalio.client.OutboundInterceptor
    ) -> temporalio.client.OutboundInterceptor:
        self.client_intercepted = True
        return super().intercept_client(next)

    def intercept_activity(
        self, next: temporalio.worker.ActivityInboundInterceptor
    ) -> temporalio.worker.ActivityInboundInterceptor:
        self.worker_intercepted = True
        return super().intercept_activity(next)

    def workflow_interceptor_class(
        self, input: temporalio.worker.WorkflowInterceptorClassInput
    ) -> type[temporalio.worker.WorkflowInboundInterceptor] | None:
        # This method gets called when the worker is configured with workflows
        # Mark that worker interceptor was used
        self.worker_intercepted = True

        # Return counting interceptor class
        call_count = self.call_count

        class CountingWorkflowInterceptor(temporalio.worker.WorkflowInboundInterceptor):
            async def execute_workflow(
                self, input: temporalio.worker.ExecuteWorkflowInput
            ):
                call_count["execute_workflow"] += 1
                return await super().execute_workflow(input)

        return CountingWorkflowInterceptor


async def test_simple_plugin_worker_interceptor_only_used_on_worker(
    client: Client,
) -> None:
    """Test that when a combined client/worker interceptor is provided by SimplePlugin
    to interceptors, and the plugin is only used on a worker (not on the client
    used to create that worker), the worker interceptor functionality is still provided."""

    interceptor = CombinedClientWorkerInterceptor()

    # Create SimplePlugin that provides the combined interceptor
    plugin = SimplePlugin(
        "TestCombinedPlugin",
        interceptors=[interceptor],
    )

    # Create worker with the plugin (but don't add plugin to client)
    worker = Worker(
        client,
        task_queue="queue" + str(uuid.uuid4()),
        activities=[never_run_activity],
        workflows=[
            HelloWorkflow
        ],  # Add workflows to trigger workflow_interceptor_class
        plugins=[plugin],
    )

    # Worker creation triggers plugin configuration
    assert worker is not None

    # The interceptor should NOT have been used for client interception
    # since the plugin was not added to the client
    assert (
        not interceptor.client_intercepted
    ), "Client interceptor should not have been used"

    # The interceptor SHOULD have been used for worker interception
    # even though it was specified in interceptors
    assert interceptor.worker_intercepted, "Worker interceptor should have been used"


async def test_simple_plugin_interceptor_duplication_when_used_on_client_and_worker(
    client: Client,
) -> None:
    """Test that when a combined client/worker interceptor is provided by SimplePlugin
    to interceptors, and the plugin is used on both client and worker,
    the interceptor is not duplicated in the worker."""

    interceptor = CombinedClientWorkerInterceptor()

    # Create SimplePlugin that provides the combined interceptor
    plugin = SimplePlugin(
        "TestCombinedPlugin",
        interceptors=[interceptor],
    )

    # Add plugin to client first
    config = client.config()
    config["plugins"] = [plugin]
    new_client = Client(**config)

    # Verify client interceptor was used
    assert interceptor.client_intercepted, "Client interceptor should have been used"

    # Reset the worker intercepted flag to test worker behavior
    interceptor.worker_intercepted = False

    # Create worker with the same plugin-enabled client
    worker = Worker(
        new_client,
        task_queue="queue" + str(uuid.uuid4()),
        activities=[never_run_activity],
        workflows=[HelloWorkflow],
    )

    # The worker interceptor functionality should still work
    # (regardless of whether it comes from client propagation or worker config)
    assert interceptor.worker_intercepted, "Worker interceptor should have been used"

    # Test execution-level duplication by running a workflow
    async with new_worker(
        new_client,
        HelloWorkflow,
        max_cached_workflows=0,
    ) as worker:
        # Start and complete a workflow
        handle = await new_client.start_workflow(
            HelloWorkflow.run,
            "test",
            id=f"counting-workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        result = await handle.result()
        assert result == "Hello, test!"

        # The workflow interceptor should only be called ONCE, not twice
        assert (
            interceptor.call_count["execute_workflow"] == 1
        ), f"Expected execute_workflow to be called once, but was called {interceptor.call_count['execute_workflow']} times. This indicates interceptor duplication in execution."


async def test_simple_plugin_no_duplication_when_interceptor_in_both_client_and_worker_params(
    client: Client,
) -> None:
    """Test that when the same interceptor is provided to the unified interceptors
    parameter in a SimplePlugin, it doesn't get duplicated."""

    interceptor = CombinedClientWorkerInterceptor()

    # Create SimplePlugin that provides the interceptor once to the unified parameter
    plugin = SimplePlugin(
        "TestCombinedPlugin",
        interceptors=[interceptor],  # Single unified parameter
    )

    # Create worker with plugin (not on client)
    worker = Worker(
        client,
        task_queue="queue" + str(uuid.uuid4()),
        activities=[never_run_activity],
        workflows=[HelloWorkflow],
        plugins=[plugin],
    )

    # The worker interceptor functionality should work
    assert interceptor.worker_intercepted, "Worker interceptor should have been used"

    # Test execution-level duplication by running a workflow
    async with worker:
        # Start and complete a workflow
        handle = await client.start_workflow(
            HelloWorkflow.run,
            "test",
            id=f"counting-workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        result = await handle.result()
        assert result == "Hello, test!"

        # The workflow interceptor should only be called ONCE, not twice
        assert (
            interceptor.call_count["execute_workflow"] == 1
        ), f"Expected execute_workflow to be called once, but was called {interceptor.call_count['execute_workflow']} times. This indicates interceptor duplication in execution."


async def test_simple_plugin_no_duplication_in_interceptor_chain(
    client: Client,
) -> None:
    """Test that interceptors don't get duplicated in the actual interceptor chain execution.
    This catches the specific OpenTelemetry issue where the same interceptor method gets called twice."""

    interceptor = CombinedClientWorkerInterceptor()

    # Create SimplePlugin that provides the combined interceptor
    plugin = SimplePlugin(
        "CountingPlugin",
        interceptors=[interceptor],
    )

    # Add plugin to client (like OpenTelemetryPlugin does)
    config = client.config()
    config["plugins"] = [plugin]
    new_client = Client(**config)

    # Create worker with the plugin-enabled client (plugin propagates from client)
    async with new_worker(
        new_client,
        HelloWorkflow,
        max_cached_workflows=0,
    ) as worker:
        # Start and complete a workflow
        handle = await new_client.start_workflow(
            HelloWorkflow.run,
            "test",
            id=f"counting-workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        result = await handle.result()
        assert result == "Hello, test!"

        # The workflow interceptor should only be called ONCE, not twice
        assert (
            interceptor.call_count["execute_workflow"] == 1
        ), f"Expected execute_workflow to be called once, but was called {interceptor.call_count['execute_workflow']} times. This indicates interceptor duplication in the chain."
