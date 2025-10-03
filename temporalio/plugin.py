"""Plugin module for Temporal SDK.

This module provides plugin functionality that allows customization of both client
and worker behavior in the Temporal SDK through configurable parameters.
"""

import abc
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Optional,
    Sequence,
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


class Plugin(temporalio.client.Plugin, temporalio.worker.Plugin, abc.ABC):
    """Abstract base class for Temporal plugins.

    This class combines both client and worker plugin capabilities,
    just used where multiple inheritance is not possible.
    """

    pass


T = TypeVar("T")

PluginParameter = Union[None, T, Callable[[Optional[T]], T]]


def create_plugin(
    name: str,
    *,
    data_converter: PluginParameter[temporalio.converter.DataConverter] = None,
    client_interceptors: PluginParameter[
        Sequence[temporalio.client.Interceptor]
    ] = None,
    activities: PluginParameter[Sequence[Callable]] = None,
    nexus_service_handlers: PluginParameter[Sequence[Any]] = None,
    workflows: PluginParameter[Sequence[Type]] = None,
    workflow_runner: PluginParameter[WorkflowRunner] = None,
    worker_interceptors: PluginParameter[
        Sequence[temporalio.worker.Interceptor]
    ] = None,
    workflow_failure_exception_types: PluginParameter[
        Sequence[Type[BaseException]]
    ] = None,
    run_context: Optional[Callable[[], AbstractAsyncContextManager[None]]] = None,
) -> Plugin:
    """Create a static plugin with configurable parameters.

    Args:
        name: The name of the plugin.
        data_converter: Data converter for serialization, or callable to customize existing one.
        client_interceptors: Client interceptors to append, or callable to customize existing ones.
        activities: Activity functions to append, or callable to customize existing ones.
        nexus_service_handlers: Nexus service handlers to append, or callable to customize existing ones.
        workflows: Workflow classes to append, or callable to customize existing ones.
        workflow_runner: Workflow runner, or callable to customize existing one.
        worker_interceptors: Worker interceptors to append, or callable to customize existing ones.
        workflow_failure_exception_types: Exception types for workflow failures to append,
            or callable to customize existing ones.
        run_context: Optional async context manager producer to wrap worker/replayer execution.

    Returns:
        A configured Plugin instance.
    """
    return _StaticPlugin(
        name=name,
        data_converter=data_converter,
        client_interceptors=client_interceptors,
        activities=activities,
        nexus_service_handlers=nexus_service_handlers,
        workflows=workflows,
        workflow_runner=workflow_runner,
        worker_interceptors=worker_interceptors,
        workflow_failure_exception_types=workflow_failure_exception_types,
        run_context=run_context,
    )


class _StaticPlugin(Plugin):
    def __init__(
        self,
        name: str,
        *,
        data_converter: PluginParameter[temporalio.converter.DataConverter],
        client_interceptors: PluginParameter[Sequence[temporalio.client.Interceptor]],
        activities: PluginParameter[Sequence[Callable]],
        nexus_service_handlers: PluginParameter[Sequence[Any]],
        workflows: PluginParameter[Sequence[Type]],
        workflow_runner: PluginParameter[WorkflowRunner],
        worker_interceptors: PluginParameter[Sequence[temporalio.worker.Interceptor]],
        workflow_failure_exception_types: PluginParameter[
            Sequence[Type[BaseException]]
        ],
        run_context: Optional[Callable[[], AbstractAsyncContextManager[None]]],
    ) -> None:
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
        return self._name

    def configure_client(self, config: ClientConfig) -> ClientConfig:
        self._set_dict(
            config,  # type: ignore
            "data_converter",
            self._resolve_parameter(config.get("data_converter"), self.data_converter),
        )
        self._set_dict(
            config,  # type: ignore
            "interceptors",
            self._resolve_append_parameter(
                config.get("interceptors"), self.client_interceptors
            ),
        )

        return config

    async def connect_service_client(
        self,
        config: ConnectConfig,
        next: Callable[[ConnectConfig], Awaitable[ServiceClient]],
    ) -> temporalio.service.ServiceClient:
        return await next(config)

    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        self._set_dict(
            config,  # type: ignore
            "activities",
            self._resolve_append_parameter(config.get("activities"), self.activities),
        )
        self._set_dict(
            config,  # type: ignore
            "nexus_service_handlers",
            self._resolve_append_parameter(
                config.get("nexus_service_handlers"), self.nexus_service_handlers
            ),
        )
        self._set_dict(
            config,  # type: ignore
            "workflows",
            self._resolve_append_parameter(config.get("workflows"), self.workflows),
        )

        self._set_dict(
            config,  # type: ignore
            "workflow_runner",
            self._resolve_parameter(
                config.get("workflow_runner"), self.workflow_runner
            ),
        )
        self._set_dict(
            config,  # type: ignore
            "interceptors",
            self._resolve_append_parameter(
                config.get("interceptors"), self.worker_interceptors
            ),
        )
        self._set_dict(
            config,  # type: ignore
            "workflow_failure_exception_types",
            self._resolve_append_parameter(
                config.get("workflow_failure_exception_types"),
                self.workflow_failure_exception_types,
            ),
        )

        return config

    def configure_replayer(self, config: ReplayerConfig) -> ReplayerConfig:
        self._set_dict(
            config,  # type: ignore
            "data_converter",
            self._resolve_parameter(config.get("data_converter"), self.data_converter),
        )
        self._set_dict(
            config,  # type: ignore
            "workflows",
            self._resolve_append_parameter(config.get("workflows"), self.workflows),
        )
        self._set_dict(
            config,  # type: ignore
            "workflow_runner",
            self._resolve_parameter(
                config.get("workflow_runner"), self.workflow_runner
            ),
        )
        self._set_dict(
            config,  # type: ignore
            "interceptors",
            self._resolve_append_parameter(
                config.get("interceptors"), self.worker_interceptors
            ),
        )
        self._set_dict(
            config,  # type: ignore
            "workflow_failure_exception_types",
            self._resolve_append_parameter(
                config.get("workflow_failure_exception_types"),
                self.workflow_failure_exception_types,
            ),
        )
        return config

    async def run_worker(
        self, worker: Worker, next: Callable[[Worker], Awaitable[None]]
    ) -> None:
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
        if self.run_context:
            async with self.run_context():
                async with next(replayer, histories) as results:
                    yield results
        else:
            async with next(replayer, histories) as results:
                yield results

    @staticmethod
    def _resolve_parameter(
        existing: Optional[T], parameter: PluginParameter[T]
    ) -> Optional[T]:
        if parameter is None:
            return existing
        elif callable(parameter):
            return cast(Callable[[Optional[T]], Optional[T]], parameter)(existing)
        else:
            return parameter

    @staticmethod
    def _resolve_append_parameter(
        existing: Optional[Sequence[T]], parameter: PluginParameter[Sequence[T]]
    ) -> Optional[Sequence[T]]:
        if parameter is None:
            return existing
        elif callable(parameter):
            return cast(
                Callable[[Optional[Sequence[T]]], Optional[Sequence[T]]], parameter
            )(existing)
        else:
            return list(existing or []) + list(parameter)

    @staticmethod
    def _set_dict(config: dict[str, Any], key: str, value: Optional[Any]) -> None:
        if value is not None:
            config[key] = value
        else:
            del config[key]
