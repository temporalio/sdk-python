import abc
import dataclasses
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, AsyncIterator, Callable, Optional, Sequence, Set, Type

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
)
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner


class Plugin(temporalio.client.Plugin, temporalio.worker.Plugin, abc.ABC):
    pass


def create_plugin(
    *,
    data_converter: Optional[temporalio.converter.DataConverter] = None,
    client_interceptors: Optional[Sequence[temporalio.client.Interceptor]] = None,
    activities: Optional[Sequence[Callable]] = None,
    nexus_service_handlers: Optional[Sequence[Any]] = None,
    workflows: Optional[Sequence[Type]] = None,
    passthrough_modules: Optional[Set[str]] = None,
    worker_interceptors: Optional[Sequence[temporalio.worker.Interceptor]] = None,
    workflow_failure_exception_types: Optional[Sequence[Type[BaseException]]] = None,
    run_context: Optional[AbstractAsyncContextManager[None]] = None,
) -> Plugin:
    return _StaticPlugin(
        data_converter=data_converter,
        client_interceptors=client_interceptors,
        activities=activities,
        nexus_service_handlers=nexus_service_handlers,
        workflows=workflows,
        passthrough_modules=passthrough_modules,
        worker_interceptors=worker_interceptors,
        workflow_failure_exception_types=workflow_failure_exception_types,
        run_context=run_context,
    )


class _StaticPlugin(Plugin):
    def __init__(
        self,
        *,
        data_converter: Optional[temporalio.converter.DataConverter] = None,
        client_interceptors: Optional[Sequence[temporalio.client.Interceptor]] = None,
        activities: Optional[Sequence[Callable]] = None,
        nexus_service_handlers: Optional[Sequence[Any]] = None,
        workflows: Optional[Sequence[Type]] = None,
        passthrough_modules: Optional[Set[str]] = None,
        worker_interceptors: Optional[Sequence[temporalio.worker.Interceptor]] = None,
        workflow_failure_exception_types: Optional[
            Sequence[Type[BaseException]]
        ] = None,
        run_context: Optional[AbstractAsyncContextManager[None]] = None,
    ) -> None:
        self.data_converter = data_converter
        self.client_interceptors = client_interceptors
        self.activities = activities
        self.nexus_service_handlers = nexus_service_handlers
        self.workflows = workflows
        self.passthrough_modules = passthrough_modules
        self.worker_interceptors = worker_interceptors
        self.workflow_failure_exception_types = workflow_failure_exception_types
        self.run_context = run_context

    def init_worker_plugin(self, next: temporalio.worker.Plugin) -> None:
        self.next_worker_plugin = next

    def init_client_plugin(self, next: temporalio.client.Plugin) -> None:
        self.next_client_plugin = next

    def configure_client(self, config: ClientConfig) -> ClientConfig:
        if self.data_converter:
            if not config["data_converter"] == temporalio.converter.default():
                raise ValueError(
                    "Static Plugin was configured with a data converter, but the client was as well."
                )
            else:
                config["data_converter"] = self.data_converter

        if self.client_interceptors:
            config["interceptors"] = list(config.get("interceptors", [])) + list(
                self.client_interceptors
            )

        return self.next_client_plugin.configure_client(config)

    async def connect_service_client(
        self, config: temporalio.service.ConnectConfig
    ) -> temporalio.service.ServiceClient:
        return await self.next_client_plugin.connect_service_client(config)

    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        if self.activities:
            config["activities"] = list(config.get("activities", [])) + list(
                self.activities
            )

        if self.nexus_service_handlers:
            config["nexus_service_handlers"] = list(
                config.get("nexus_service_handlers", [])
            ) + list(self.nexus_service_handlers)

        if self.workflows:
            config["workflows"] = list(config.get("workflows", [])) + list(
                self.workflows
            )

        if self.passthrough_modules:
            runner = config.get("workflow_runner")
            if runner and isinstance(runner, SandboxedWorkflowRunner):
                config["workflow_runner"] = dataclasses.replace(
                    runner,
                    restrictions=runner.restrictions.with_passthrough_modules(
                        *self.passthrough_modules
                    ),
                )

        if self.worker_interceptors:
            config["interceptors"] = list(config.get("interceptors", [])) + list(
                self.worker_interceptors
            )

        if self.workflow_failure_exception_types:
            config["workflow_failure_exception_types"] = list(
                config.get("workflow_failure_exception_types", [])
            ) + list(self.workflow_failure_exception_types)

        return config

    def configure_replayer(self, config: ReplayerConfig) -> ReplayerConfig:
        if self.data_converter:
            if not config["data_converter"] == temporalio.converter.default():
                raise ValueError(
                    "Static Plugin was configured with a data converter, but the client was as well."
                )
            else:
                config["data_converter"] = self.data_converter

        if self.workflows:
            config["workflows"] = list(config.get("workflows", [])) + list(
                self.workflows
            )

        if self.passthrough_modules:
            runner = config.get("workflow_runner")
            if runner and isinstance(runner, SandboxedWorkflowRunner):
                config["workflow_runner"] = dataclasses.replace(
                    runner,
                    restrictions=runner.restrictions.with_passthrough_modules(
                        *self.passthrough_modules
                    ),
                )

        if self.worker_interceptors:
            config["interceptors"] = list(config.get("interceptors", [])) + list(
                self.worker_interceptors
            )

        if self.workflow_failure_exception_types:
            config["workflow_failure_exception_types"] = list(
                config.get("workflow_failure_exception_types", [])
            ) + list(self.workflow_failure_exception_types)

        return config

    async def run_worker(self, worker: Worker) -> None:
        if self.run_context:
            async with self.run_context:
                await self.next_worker_plugin.run_worker(worker)
        else:
            await self.next_worker_plugin.run_worker(worker)

    @asynccontextmanager
    async def run_replayer(
        self,
        replayer: Replayer,
        histories: AsyncIterator[WorkflowHistory],
    ) -> AsyncIterator[AsyncIterator[WorkflowReplayResult]]:
        if self.run_context:
            async with self.run_context:
                async with self.next_worker_plugin.run_replayer(
                    replayer, histories
                ) as results:
                    yield results
        else:
            async with self.next_worker_plugin.run_replayer(
                replayer, histories
            ) as results:
                yield results
