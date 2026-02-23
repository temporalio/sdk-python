"""
Test that the Nexus SDK can be used to define an operation that responds asynchronously.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

import pytest
from nexusrpc.handler import (
    CancelOperationContext,
    OperationHandler,
    StartOperationContext,
    StartOperationResultAsync,
    service_handler,
)
from nexusrpc.handler._decorators import operation_handler

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers.nexus import ServiceClient, make_nexus_endpoint_name


@dataclass
class Input:
    value: str


@dataclass
class Output:
    value: str


@dataclass
class AsyncOperationWithAsyncDefs(OperationHandler[Input, Output]):
    executor: TaskExecutor

    async def start(
        self, ctx: StartOperationContext, input: Input
    ) -> StartOperationResultAsync:
        async def task() -> Output:
            await asyncio.sleep(0.1)
            return Output("Hello from async operation!")

        task_id = str(uuid.uuid4())
        await self.executor.add_task(task_id, task())
        return StartOperationResultAsync(token=task_id)

    async def cancel(self, ctx: CancelOperationContext, token: str) -> None:
        self.executor.request_cancel_task(task_id=token)


@dataclass
class AsyncOperationWithNonAsyncDefs(OperationHandler[Input, Output]):
    executor: TaskExecutor

    def start(
        self, ctx: StartOperationContext, input: Input
    ) -> StartOperationResultAsync:
        async def task() -> Output:
            await asyncio.sleep(0.1)
            return Output("Hello from async operation!")

        task_id = str(uuid.uuid4())
        self.executor.add_task_sync(task_id, task())
        return StartOperationResultAsync(token=task_id)

    def cancel(self, ctx: CancelOperationContext, token: str) -> None:
        self.executor.request_cancel_task(task_id=token)


@dataclass
@service_handler
class MyServiceHandlerWithAsyncDefs:
    executor: TaskExecutor

    @operation_handler
    def async_operation(self) -> OperationHandler[Input, Output]:
        return AsyncOperationWithAsyncDefs(self.executor)


@dataclass
@service_handler
class MyServiceHandlerWithNonAsyncDefs:
    executor: TaskExecutor

    @operation_handler
    def async_operation(self) -> OperationHandler[Input, Output]:
        return AsyncOperationWithNonAsyncDefs(self.executor)


@pytest.mark.parametrize(
    "service_handler_cls",
    [
        MyServiceHandlerWithAsyncDefs,
        MyServiceHandlerWithNonAsyncDefs,
    ],
)
async def test_async_operation_lifecycle(
    env: WorkflowEnvironment,
    service_handler_cls: (
        type[MyServiceHandlerWithAsyncDefs] | type[MyServiceHandlerWithNonAsyncDefs]
    ),
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_executor = await TaskExecutor.connect()
    task_queue = str(uuid.uuid4())
    endpoint = (
        await env.create_nexus_endpoint(
            make_nexus_endpoint_name(task_queue), task_queue
        )
    ).id
    service_client = ServiceClient(
        ServiceClient.default_server_address(env),
        endpoint,
        service_handler_cls.__name__,
    )

    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_service_handlers=[service_handler_cls(task_executor)],
        nexus_task_executor=concurrent.futures.ThreadPoolExecutor(),
    ):
        start_response = await service_client.start_operation(
            "async_operation",
            body=dataclass_as_dict(Input(value="Hello from test")),
        )
        assert start_response.status_code == 201
        assert start_response.json()["token"]
        assert start_response.json()["state"] == "running"

        # Cancel it
        cancel_response = await service_client.cancel_operation(
            "async_operation",
            token=start_response.json()["token"],
        )
        assert cancel_response.status_code == 202

        # get_info and get_result not implemented by server


@dataclass
class TaskExecutor:
    """
    This class represents the task execution platform being used by the team operating the
    Nexus operation.
    """

    event_loop: asyncio.AbstractEventLoop
    tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict)

    @classmethod
    async def connect(cls) -> TaskExecutor:
        return cls(event_loop=asyncio.get_running_loop())

    async def add_task(self, task_id: str, coro: Coroutine[Any, Any, Any]) -> None:
        """
        Add a task to the task execution platform.
        """
        if task_id in self.tasks:
            raise RuntimeError(f"Task with id {task_id} already exists")

        # This function is async def because in reality this step will often write to
        # durable storage.
        self.tasks[task_id] = asyncio.create_task(coro)

    def add_task_sync(self, task_id: str, coro: Coroutine[Any, Any, Any]) -> None:
        """
        Add a task to the task execution platform from a sync context.
        """
        asyncio.run_coroutine_threadsafe(
            self.add_task(task_id, coro), self.event_loop
        ).result()

    async def get_task_result(self, task_id: str) -> Any:
        """
        Get the result of a task from the task execution platform.
        """
        task = self.tasks.get(task_id)
        if not task:
            raise RuntimeError(f"Task not found with id {task_id}")
        return await task

    def get_task_result_sync(self, task_id: str) -> Any:
        """
        Get the result of a task from the task execution platform from a sync context.
        """
        return asyncio.run_coroutine_threadsafe(
            self.get_task_result(task_id), self.event_loop
        ).result()

    def request_cancel_task(self, task_id: str) -> None:
        """
        Request cancellation of a task on the task execution platform.
        """
        task = self.tasks.get(task_id)
        if not task:
            raise RuntimeError(f"Task not found with id {task_id}")
        task.cancel()
        # Not implemented: cancellation confirmation, deletion on cancellation


def dataclass_as_dict(dataclass: Any) -> dict[str, Any]:
    """
    Return a shallow dict of the dataclass's fields.

    dataclasses.as_dict goes too far (attempts to pickle values)
    """
    return {
        field.name: getattr(dataclass, field.name)
        for field in dataclasses.fields(dataclass)
    }
