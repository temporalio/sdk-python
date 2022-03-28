
from __future__ import annotations

import abc
import asyncio
import logging
from typing import (
    Awaitable,
    Callable,
    Dict,
    Iterable,
    Type,
)

import temporalio.activity
import temporalio.api.common.v1
import temporalio.bridge.client
import temporalio.bridge.proto
import temporalio.bridge.proto.workflow_activation
import temporalio.bridge.proto.workflow_completion
import temporalio.bridge.proto.common
import temporalio.bridge.worker
import temporalio.client
import temporalio.converter
import temporalio.exceptions
import temporalio.workflow_service

from .interceptor import (
    Interceptor,
)

logger = logging.getLogger(__name__)

class _WorkflowWorker:
    def __init__(
        self,
        *,
        bridge_worker: Callable[[], temporalio.bridge.worker.Worker],
        task_queue: str,
        workflows: Iterable[Type],
        type_hint_eval_str: bool,
        data_converter: temporalio.converter.DataConverter,
        interceptors: Iterable[Interceptor],
    ) -> None:
        self._bridge_worker = bridge_worker
        self._task_queue = task_queue
        self._data_converter = data_converter
        self._interceptors = interceptors
        self._running_workflows: Dict[str, RunningWorkflow] = {}
        self._comp_queue: asyncio.Queue[temporalio.bridge.proto.workflow_completion.WorkflowActivationCompletion] = asyncio.Queue(1000)

        # TODO(cretz): Validate workflows

    async def run(self) -> None:
        # Continually poll for workflow work
        poll_task = asyncio.create_task(self._bridge_worker().poll_workflow_activation())
        comp_task = asyncio.create_task(self._comp_queue.get())
        tasks = [poll_task, comp_task]
        while True:
            try:
                # Wait for poll or completion
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if poll_task in done:
                    await self._handle_activation(await poll_task)
                    poll_task = asyncio.create_task(self._bridge_worker().poll_workflow_activation())
                    tasks[0] = poll_task
                if comp_task in done:
                    await self._handle_completion(await comp_task)
                    comp_task = asyncio.create_task(self._comp_queue.get())
                    tasks[1] = comp_task
            except temporalio.bridge.worker.PollShutdownError:
                return
            except Exception:
                # Should never happen
                logger.exception(f"Workflow runner failed")

    async def _handle_activation(self, act: temporalio.bridge.proto.workflow_activation.WorkflowActivation) -> None:
        try:
            # If in running set, just send the activation
            workflow = self._running_workflows.get(act.run_id)
            if workflow:
                await workflow.on_activation(act)
                return

            # Since it's not running, first find the start workflow job
            start_job = next((j for j in act.jobs if j.HasField("start_workflow")), None)
            if not start_job:
                raise RuntimeError("Missing start workflow")
            raise NotImplementedError
        except:
            # TODO
            pass

    async def _handle_completion(self, comp: temporalio.bridge.proto.workflow_completion.WorkflowActivationCompletion) -> None:
        pass

class WorkflowRunner(abc.ABC):

    async def new_workflow(self, cls: Type, comp: Callable[[temporalio.bridge.proto.workflow_completion.WorkflowActivationCompletion], Awaitable[None]]) -> RunningWorkflow:
        raise NotImplementedError

class RunningWorkflow(abc.ABC):

    async def on_activation(self, act: temporalio.bridge.proto.workflow_activation.WorkflowActivation) -> None:
        raise NotImplementedError
