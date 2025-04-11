import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Generic, Optional, TypeVar

import nexusrpc.handler

from temporalio.client import (
    Client,
)
from temporalio.nexus.handler import AsyncWorkflowOperationResult
from temporalio.types import (
    CallableAsyncSingleParam,
    MethodAsyncSingleParam,
)
from temporalio.worker._workflow_instance import _ActivityHandle

O = TypeVar("O")
I = TypeVar("I")


@dataclass
class WorkflowRunOperation(Generic[I, O]):
    workflow_run_method: MethodAsyncSingleParam[Any, I, O]
    task_queue: str

    @classmethod
    def from_workflow_run_method(
        cls, workflow_run_method: MethodAsyncSingleParam[Any, I, O]
    ) -> "WorkflowRunOperation[I, O]":
        """
        Define an async op backed by a workflow.

        Args:
            workflow_run_method: @workflow.run method
        """
        # TODO default task_queue to this worker's task queue
        task_queue = "default"
        return cls(workflow_run_method, task_queue)

    async def start(self, input: I) -> AsyncWorkflowOperationResult[O]:
        # TODO obtain this worker's client
        client = await Client.connect(target_host="localhost:7233")
        workflow_handle = await client.start_workflow(
            self.workflow_run_method,
            input,
            id=str(uuid.uuid4()),
            task_queue=self.task_queue,
        )
        return AsyncWorkflowOperationResult.from_workflow_handle(workflow_handle)


def workflow_operation(
    start_method: Callable[
        [Any, I, nexusrpc.handler.StartOperationOptions],
        Awaitable[AsyncWorkflowOperationResult[O]],
    ],
) -> nexusrpc.handler.Operation[I, AsyncWorkflowOperationResult[O]]:
    """
    A decorator that creates an operation handler from a workflow start method.
    """

    @dataclass
    class WorkflowOperationHandler:
        workflow_id: str

        # @wraps(start_method)
        async def start(
            self, input: I, options: nexusrpc.handler.StartOperationOptions
        ) -> AsyncWorkflowOperationResult[O]:
            return await start_method(None, input, options)

        async def cancel(self) -> None:
            raise NotImplementedError

        async def fetch_info(self) -> nexusrpc.handler.OperationInfo:
            raise NotImplementedError

        @property
        def operation_id(self) -> str:
            # TODO: are we returning workflow IDs as operation IDs?
            return self.workflow_id

    return WorkflowOperationHandler(workflow_id=str(uuid.uuid4()))


# TODO: see AsyncOperationResult in Nexus SDK
@dataclass
class AsyncOperationResult(Generic[O]):
    operation_id: list[Optional[str]]

    @classmethod
    def from_workflow_handle(
        cls, workflow_handle: WorkflowHandle[Any, O]
    ) -> "AsyncOperationResult[O]":
        return cls([workflow_handle.id, workflow_handle.run_id])

    @classmethod
    def from_activity_handle(
        cls, activity_handle: ActivityHandle[O]
    ) -> "AsyncOperationResult[O]":
        # TODO: standalone activity ID
        activity_id = "<activity-id>"
        return cls([activity_id])

    @classmethod
    def from_update_handle(
        cls, update_handle: WorkflowUpdateHandle[O]
    ) -> "AsyncOperationResult[O]":
        return cls(
            [
                update_handle.workflow_id,
                update_handle.workflow_run_id,
                update_handle.id,
            ]
        )


def update_operation(x: Any) -> Any:
    return x


def activity_operation(x: Any) -> Any:
    return x


def workflow_id_from_input(input: Any) -> str:
    # TODO
    return str(uuid.uuid4())


async def start_activity(
    activity_method: CallableAsyncSingleParam[I, O],
    input: I,
) -> nexusrpc.handler.AsyncOperationResult:
    namespace = "my-target-namespace"  # TODO
    client = await Client.connect(target_host="localhost:7233", namespace=namespace)
    activity_handle: _ActivityHandle[O] = await client.start_standalone_activity(
        activity_method, input
    )
    return nexusrpc.handler.AsyncOperationResult.from_activity_handle(activity_handle)


async def start_update(
    update_method: UpdateMethodMultiParam[[Any, I], O],
    input: I,
) -> nexusrpc.handler.AsyncOperationResult:
    client = await Client.connect(target_host="localhost:7233")
    workflow_handle = await client.start_workflow(
        "workflow-type-used-for-update",  # TODO
        id=workflow_id_from_input(input),
        task_queue="my-handler-task-queue",  # TODO
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )
    update_handle = await workflow_handle.start_update(
        update_method,
        arg=input,
        id="my-update-id",
        wait_for_stage=WorkflowUpdateStage.ACCEPTED,
    )
    return nexusrpc.handler.AsyncOperationResult.from_update_handle(update_handle)
