import abc
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, AsyncIterator, Callable, Sequence, Type, TypedDict, TypeVar

import temporalio.client
import temporalio.converter
import temporalio.worker
from temporalio.client import ClientConfig, WorkflowHistory
from temporalio.worker import (
    Replayer,
    ReplayerConfig,
    Worker,
    WorkerConfig,
    WorkflowReplayResult,
    WorkflowRunner,
)


class PluginConfig(TypedDict, total=False):
    data_converter: temporalio.converter.DataConverter
    client_interceptors: Sequence[temporalio.client.Interceptor]
    worker_interceptors: Sequence[temporalio.worker.Interceptor]
    activities: Sequence[Callable]
    nexus_service_handlers: Sequence[Any]
    workflows: Sequence[Type]
    workflow_runner: WorkflowRunner


class Plugin(
    temporalio.client.LowLevelPlugin, temporalio.worker.LowLevelPlugin, abc.ABC
):
    def init_worker_plugin(self, next: temporalio.worker.LowLevelPlugin) -> None:
        self.next_worker_plugin = next

    def init_client_plugin(self, next: temporalio.client.LowLevelPlugin) -> None:
        self.next_client_plugin = next

    def configure_client(self, config: ClientConfig) -> ClientConfig:
        plugin_config = self.configuration()

        new_converter = plugin_config.get("data_converter")
        if new_converter:
            if not config["data_converter"] == temporalio.converter.default():
                config["data_converter"] = self.resolve_collision(
                    config["data_converter"], new_converter
                )
            else:
                config["data_converter"] = new_converter

        client_interceptors = plugin_config.get("client_interceptors")
        if client_interceptors:
            config["interceptors"] = list(config.get("interceptors", [])) + list(
                client_interceptors
            )
        return self.next_client_plugin.configure_client(config)

    async def connect_service_client(
        self, config: temporalio.service.ConnectConfig
    ) -> temporalio.service.ServiceClient:
        return await self.next_client_plugin.connect_service_client(config)

    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        plugin_config = self.configuration()

        worker_interceptors = plugin_config.get("worker_interceptors")
        if worker_interceptors:
            config["interceptors"] = list(config.get("interceptors", [])) + list(
                worker_interceptors
            )

        activities = plugin_config.get("activities")
        if activities:
            config["activities"] = list(config.get("activities", [])) + list(activities)

        nexus_service_handlers = plugin_config.get("nexus_service_handlers")
        if nexus_service_handlers:
            config["nexus_service_handlers"] = list(
                config.get("nexus_service_handlers", [])
            ) + list(nexus_service_handlers)

        workflows = plugin_config.get("workflows")
        if workflows:
            config["workflows"] = list(config.get("workflows", [])) + list(workflows)

        workflow_runner = plugin_config.get("workflow_runner")
        if workflow_runner:
            old_runner = config.get("workflow_runner")
            if old_runner:
                config["workflow_runner"] = self.resolve_collision(
                    old_runner, workflow_runner
                )
            else:
                config["workflow_runner"] = workflow_runner
        return config

    def configure_replayer(self, config: ReplayerConfig) -> ReplayerConfig:
        plugin_config = self.configuration()

        new_converter = plugin_config.get("data_converter")
        if new_converter:
            old_converter = config.get("data_converter")
            if old_converter and not old_converter == temporalio.converter.default():
                config["data_converter"] = self.resolve_collision(
                    old_converter, new_converter
                )
            else:
                config["data_converter"] = new_converter

        worker_interceptors = plugin_config.get("worker_interceptors")
        if worker_interceptors:
            config["interceptors"] = list(config.get("interceptors", [])) + list(
                worker_interceptors
            )

        workflows = plugin_config.get("workflows")
        if workflows:
            config["workflows"] = list(config.get("workflows", [])) + list(workflows)

        workflow_runner = plugin_config.get("workflow_runner")
        if workflow_runner:
            old_runner = config.get("workflow_runner")
            if old_runner:
                config["workflow_runner"] = self.resolve_collision(
                    old_runner, workflow_runner
                )
            else:
                config["workflow_runner"] = workflow_runner
        return config

    async def run_worker(self, worker: Worker) -> None:
        async with self.run_context():
            await self.next_worker_plugin.run_worker(worker)

    @asynccontextmanager
    async def run_replayer(
        self,
        replayer: Replayer,
        histories: AsyncIterator[WorkflowHistory],
    ) -> AsyncIterator[AsyncIterator[WorkflowReplayResult]]:
        async with self.run_context():
            async with self.next_worker_plugin.run_replayer(replayer, histories) as results:
                yield results

    @abc.abstractmethod
    def run_context(self) -> AbstractAsyncContextManager[None]:
        raise NotImplementedError()

    @abc.abstractmethod
    def configuration(self) -> PluginConfig:
        raise NotImplementedError()

    T = TypeVar("T")

    def resolve_collision(
        self,
        old: T,
        new: T,
    ) -> T:
        """How to handle cases where an option is already set by the user or an earlier plugin.
        The default implementation is to fail, but it can be overridden."""
        raise ValueError(f"{old} is already set, plugin cannot reasonable override.")
