"""Plugin module for Temporal SDK.

This module provides plugin functionality that allows customization of both client
and worker behavior in the Temporal SDK through configurable parameters.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import (
    Any,
    Optional,
    Type,
    TypeVar,
    Union,
    cast,
)

import temporalio.client
import temporalio.converter
import temporalio.worker
from temporalio.client import ClientConfig, WorkflowHistory
from temporalio.service import ConnectConfig, ServiceClient
from temporalio.worker import (
    Replayer,
    ReplayerConfig,
    Worker,
    WorkerConfig,
    WorkflowReplayResult,
    WorkflowRunner,
)

T = TypeVar("T")

PluginParameter = Union[None, T, Callable[[Optional[T]], T]]


class SimplePlugin(temporalio.client.Plugin, temporalio.worker.Plugin):
    """A simple plugin definition which has a limited set of configurations but makes it easier to produce
    a plugin which needs to configure them.
    """

    def __init__(
        self,
        name: str,
        *,
        data_converter: PluginParameter[temporalio.converter.DataConverter] = None,
        client_interceptors: PluginParameter[
            Sequence[temporalio.client.Interceptor]
        ] = None,
        activities: PluginParameter[Sequence[Callable]] = None,
        nexus_service_handlers: PluginParameter[Sequence[Any]] = None,
        workflows: PluginParameter[Sequence[type]] = None,
        workflow_runner: PluginParameter[WorkflowRunner] = None,
        worker_interceptors: PluginParameter[
            Sequence[temporalio.worker.Interceptor]
        ] = None,
        workflow_failure_exception_types: PluginParameter[
            Sequence[type[BaseException]]
        ] = None,
        run_context: Callable[[], AbstractAsyncContextManager[None]] | None = None,
    ) -> None:
        """Create a simple plugin with configurable parameters. Each of the parameters will be applied to any
            component for which they are applicable. All arguments are optional, and all but run_context can also
            be callables for more complex modification. See the type PluginParameter above.
            For details on each argument, see below.

        Args:
            name: The name of the plugin.
            data_converter: Data converter for serialization, or callable to customize existing one.
                Applied to the Client and Replayer.
            client_interceptors: Client interceptors to append, or callable to customize existing ones.
                Applied to the Client. Note, if the provided interceptor is also a worker.Interceptor,
                it will be added to any worker which uses that client.
            activities: Activity functions to append, or callable to customize existing ones.
                Applied to the Worker.
            nexus_service_handlers: Nexus service handlers to append, or callable to customize existing ones.
                Applied to the Worker.
            workflows: Workflow classes to append, or callable to customize existing ones.
                Applied to the Worker and Replayer.
            workflow_runner: Workflow runner, or callable to customize existing one.
                Applied to the Worker and Replayer.
            worker_interceptors: Worker interceptors to append, or callable to customize existing ones.
                Applied to the Worker and Replayer.
            workflow_failure_exception_types: Exception types for workflow failures to append,
                or callable to customize existing ones. Applied to the Worker and Replayer.
            run_context: A place to run custom code to wrap around the Worker (or Replayer) execution.
                Specifically, it's an async context manager producer. Applied to the Worker and Replayer.

        Returns:
            A configured Plugin instance.
        """
        self._name = name
        self.data_converter = data_converter
        self.client_interceptors = client_interceptors
        self.activities = activities
        self.nexus_service_handlers = nexus_service_handlers
        self.workflows = workflows
        self.workflow_runner = workflow_runner
        self.worker_interceptors = worker_interceptors
        self.workflow_failure_exception_types = workflow_failure_exception_types
        self.run_context = run_context

    def name(self) -> str:
        """See base class."""
        return self._name

    def configure_client(self, config: ClientConfig) -> ClientConfig:
        """See base class."""
        data_converter = _resolve_parameter(
            config.get("data_converter"), self.data_converter
        )
        if data_converter:
            config["data_converter"] = data_converter

        interceptors = _resolve_append_parameter(
            config.get("interceptors"), self.client_interceptors
        )
        if interceptors is not None:
            config["interceptors"] = interceptors

        return config

    async def connect_service_client(
        self,
        config: ConnectConfig,
        next: Callable[[ConnectConfig], Awaitable[ServiceClient]],
    ) -> temporalio.service.ServiceClient:
        """See base class."""
        return await next(config)

    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        """See base class."""
        activities = _resolve_append_parameter(
            config.get("activities"), self.activities
        )
        if activities:
            config["activities"] = activities

        nexus_service_handlers = _resolve_append_parameter(
            config.get("nexus_service_handlers"), self.nexus_service_handlers
        )
        if nexus_service_handlers is not None:
            config["nexus_service_handlers"] = nexus_service_handlers

        workflows = _resolve_append_parameter(config.get("workflows"), self.workflows)
        if workflows is not None:
            config["workflows"] = workflows

        workflow_runner = _resolve_parameter(
            config.get("workflow_runner"), self.workflow_runner
        )
        if workflow_runner:
            config["workflow_runner"] = workflow_runner

        interceptors = _resolve_append_parameter(
            config.get("interceptors"), self.worker_interceptors
        )
        if interceptors is not None:
            config["interceptors"] = interceptors

        failure_exception_types = _resolve_append_parameter(
            config.get("workflow_failure_exception_types"),
            self.workflow_failure_exception_types,
        )
        if failure_exception_types is not None:
            config["workflow_failure_exception_types"] = failure_exception_types

        return config

    def configure_replayer(self, config: ReplayerConfig) -> ReplayerConfig:
        """See base class."""
        data_converter = _resolve_parameter(
            config.get("data_converter"), self.data_converter
        )
        if data_converter:
            config["data_converter"] = data_converter

        workflows = _resolve_append_parameter(config.get("workflows"), self.workflows)
        if workflows is not None:
            config["workflows"] = workflows

        workflow_runner = _resolve_parameter(
            config.get("workflow_runner"), self.workflow_runner
        )
        if workflow_runner:
            config["workflow_runner"] = workflow_runner

        interceptors = _resolve_append_parameter(
            config.get("interceptors"), self.worker_interceptors
        )
        if interceptors is not None:
            config["interceptors"] = interceptors

        failure_exception_types = _resolve_append_parameter(
            config.get("workflow_failure_exception_types"),
            self.workflow_failure_exception_types,
        )
        if failure_exception_types is not None:
            config["workflow_failure_exception_types"] = failure_exception_types

        return config

    async def run_worker(
        self, worker: Worker, next: Callable[[Worker], Awaitable[None]]
    ) -> None:
        """See base class."""
        if self.run_context:
            async with self.run_context():
                await next(worker)
        else:
            await next(worker)

    @asynccontextmanager
    async def run_replayer(
        self,
        replayer: Replayer,
        histories: AsyncIterator[WorkflowHistory],
        next: Callable[
            [Replayer, AsyncIterator[WorkflowHistory]],
            AbstractAsyncContextManager[AsyncIterator[WorkflowReplayResult]],
        ],
    ) -> AsyncIterator[AsyncIterator[WorkflowReplayResult]]:
        """See base class."""
        if self.run_context:
            async with self.run_context():
                async with next(replayer, histories) as results:
                    yield results
        else:
            async with next(replayer, histories) as results:
                yield results


def _resolve_parameter(existing: T | None, parameter: PluginParameter[T]) -> T | None:
    if parameter is None:
        return existing
    elif callable(parameter):
        return cast(Callable[[Optional[T]], Optional[T]], parameter)(existing)
    else:
        return parameter


def _resolve_append_parameter(
    existing: Sequence[T] | None, parameter: PluginParameter[Sequence[T]]
) -> Sequence[T] | None:
    if parameter is None:
        return existing
    elif callable(parameter):
        return cast(
            Callable[[Optional[Sequence[T]]], Optional[Sequence[T]]], parameter
        )(existing)
    else:
        return list(existing or []) + list(parameter)
