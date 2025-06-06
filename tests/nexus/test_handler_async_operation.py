"""
Test that the Nexus SDK can be used to define an operation that responds asynchronously.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

import nexusrpc
import nexusrpc.handler
from nexusrpc.handler import (
    CancelOperationContext,
    FetchOperationInfoContext,
    FetchOperationResultContext,
    OperationHandler,
    OperationInfo,
    StartOperationContext,
    StartOperationResultAsync,
)
from nexusrpc.testing.client import ServiceClient

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers.nexus import create_nexus_endpoint


@dataclass
class Input:
    value: str


@dataclass
class Output:
    value: str


@dataclass
class AsyncOperation(OperationHandler[Input, Output]):
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

    async def fetch_info(
        self, ctx: FetchOperationInfoContext, token: str
    ) -> OperationInfo:
        status = self.executor.get_task_status(task_id=token)
        return OperationInfo(token=token, status=status)

    async def fetch_result(
        self, ctx: FetchOperationResultContext, token: str
    ) -> Output:
        return await self.executor.get_task_result(task_id=token)

    async def cancel(self, ctx: CancelOperationContext, token: str) -> None:
        self.executor.request_cancel_task(task_id=token)


@dataclass
@nexusrpc.handler.service_handler
class MyServiceHandler:
    executor: TaskExecutor

    @nexusrpc.handler.operation_handler
    def async_operation(self) -> OperationHandler[Input, Output]:
        return AsyncOperation(self.executor)


async def test_async_operation_lifecycle(env: WorkflowEnvironment):
    task_executor = await TaskExecutor.connect()
    task_queue = str(uuid.uuid4())
    endpoint = (await create_nexus_endpoint(task_queue, env.client)).endpoint.id
    service_client = ServiceClient(
        f"http://127.0.0.1:{env._http_port}",  # type: ignore
        endpoint,
        MyServiceHandler.__name__,
    )

    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_services=[MyServiceHandler(task_executor)],
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

    tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict)

    @classmethod
    async def connect(cls) -> TaskExecutor:
        return cls()

    async def add_task(self, task_id: str, coro: Coroutine[Any, Any, Any]) -> None:
        """
        Add a task to the task execution platform.
        """
        if task_id in self.tasks:
            raise RuntimeError(f"Task with id {task_id} already exists")

        # This function is async def because in reality this step will often write to
        # durable storage.
        self.tasks[task_id] = asyncio.create_task(coro)

    def get_task_status(self, task_id: str) -> nexusrpc.handler.OperationState:
        task = self.tasks[task_id]
        if not task.done():
            return nexusrpc.handler.OperationState.RUNNING
        elif task.cancelled():
            return nexusrpc.handler.OperationState.CANCELED
        elif task.exception():
            return nexusrpc.handler.OperationState.FAILED
        else:
            return nexusrpc.handler.OperationState.SUCCEEDED

    async def get_task_result(self, task_id: str) -> Any:
        """
        Get the result of a task from the task execution platform.
        """
        task = self.tasks.get(task_id)
        if not task:
            raise RuntimeError(f"Task not found with id {task_id}")
        return await task

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
