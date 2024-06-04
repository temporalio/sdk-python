"""Worker helper used for testing.

This invokes a "Kitchen Sink" workflow in an external worker. KS-prefixed class
names refer to types for the "Kitchen Sink" workflow.
"""

from __future__ import annotations

import asyncio
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional, Sequence, Tuple

import temporalio.converter
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


@dataclass
class KSWorkflowParams:
    actions: Optional[Sequence[KSAction]] = None
    action_signal: Optional[str] = None


@dataclass
class KSAction:
    result: Optional[KSResultAction] = None
    error: Optional[KSErrorAction] = None
    continue_as_new: Optional[KSContinueAsNewAction] = None
    sleep: Optional[KSSleepAction] = None
    query_handler: Optional[KSQueryHandlerAction] = None
    signal: Optional[KSSignalAction] = None
    execute_activity: Optional[KSExecuteActivityAction] = None


@dataclass
class KSResultAction:
    value: Optional[Any] = None
    run_id: Optional[bool] = None


@dataclass
class KSErrorAction:
    message: Optional[str] = None
    details: Optional[Any] = None
    attempt: Optional[bool] = None


@dataclass
class KSContinueAsNewAction:
    while_above_zero: int


@dataclass
class KSSleepAction:
    millis: int


@dataclass
class KSQueryHandlerAction:
    name: str


@dataclass
class KSSignalAction:
    name: str


@dataclass
class KSExecuteActivityAction:
    name: str
    task_queue: Optional[str] = None
    args: Optional[Sequence[Any]] = None
    count: Optional[int] = None
    index_as_arg: Optional[bool] = None
    schedule_to_close_timeout_ms: Optional[int] = None
    start_to_close_timeout_ms: Optional[int] = None
    schedule_to_start_timeout_ms: Optional[int] = None
    cancel_after_ms: Optional[int] = None
    wait_for_cancellation: Optional[bool] = None
    heartbeat_timeout_ms: Optional[int] = None
    retry_max_attempts: Optional[int] = None
    non_retryable_error_types: Optional[Sequence[str]] = None


@workflow.defn(name="kitchen_sink")
class KitchenSinkWorkflow:
    @workflow.run
    async def run(self, params: KSWorkflowParams) -> Any:
        # Handle all initial actions
        for action in params.actions or []:
            should_return, ret = await self.handle_action(params, action)
            if should_return:
                return ret
        # Handle signal actions
        if params.action_signal:
            action_queue: asyncio.Queue[KSAction] = asyncio.Queue()
            workflow.set_signal_handler(params.action_signal, action_queue.put_nowait)
            while True:
                raw_action = await action_queue.get()
                action = temporalio.converter.value_to_type(KSAction, raw_action)
                should_return, ret = await self.handle_action(params, action)
                if should_return:
                    return ret

    async def handle_action(
        self, params: KSWorkflowParams, action: KSAction
    ) -> Tuple[bool, Any]:
        if action.result:
            if action.result.run_id:
                return (True, workflow.info().run_id)
            return True, action.result.value
        elif action.error:
            if action.error.attempt:
                raise ApplicationError(f"attempt {workflow.info().attempt}")
            details = [action.error.details] if action.error.details else []
            raise ApplicationError(action.error.message or "", *details)
        elif action.continue_as_new:
            if action.continue_as_new.while_above_zero > 0:
                action.continue_as_new.while_above_zero -= 1
                workflow.continue_as_new(params)
        elif action.sleep:
            await asyncio.sleep(action.sleep.millis / 1000.0)
        elif action.query_handler:
            workflow.set_query_handler(action.query_handler.name, lambda v: v)
        elif action.signal:
            signal_event = asyncio.Event()

            def signal_handler(arg: Optional[Any] = None) -> None:
                signal_event.set()

            workflow.set_signal_handler(action.signal.name, signal_handler)
            await signal_event.wait()
        elif action.execute_activity:
            opt = action.execute_activity
            config = workflow.ActivityConfig(
                task_queue=opt.task_queue,
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(milliseconds=1),
                    backoff_coefficient=1.01,
                    maximum_interval=timedelta(milliseconds=2),
                    maximum_attempts=opt.retry_max_attempts or 1,
                    non_retryable_error_types=opt.non_retryable_error_types or [],
                ),
            )
            if opt.schedule_to_close_timeout_ms:
                config["schedule_to_close_timeout"] = timedelta(
                    milliseconds=opt.schedule_to_close_timeout_ms
                )
            elif not opt.start_to_close_timeout_ms:
                config["start_to_close_timeout"] = timedelta(minutes=3)
            if opt.start_to_close_timeout_ms:
                config["start_to_close_timeout"] = timedelta(
                    milliseconds=opt.start_to_close_timeout_ms
                )
            if opt.schedule_to_start_timeout_ms:
                config["schedule_to_start_timeout"] = timedelta(
                    milliseconds=opt.schedule_to_start_timeout_ms
                )
            if opt.wait_for_cancellation:
                config[
                    "cancellation_type"
                ] = workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED
            if opt.heartbeat_timeout_ms:
                config["heartbeat_timeout"] = timedelta(
                    milliseconds=opt.heartbeat_timeout_ms
                )
            config["retry_policy"] = RetryPolicy(
                initial_interval=timedelta(milliseconds=1),
                backoff_coefficient=1.01,
                maximum_interval=timedelta(milliseconds=2),
                maximum_attempts=opt.retry_max_attempts or 1,
                non_retryable_error_types=opt.non_retryable_error_types or [],
            )
            # Start them all
            last_result = None

            async def run_activity(index: int) -> None:
                nonlocal last_result
                args = opt.args or []
                if opt.index_as_arg:
                    args = [index]
                task = workflow.start_activity(opt.name, args=args, **config)
                if opt.cancel_after_ms:
                    asyncio.create_task(
                        cancel_after(task, opt.cancel_after_ms / 1000.0)
                    )
                last_result = await task

            pending_tasks = []
            for i in range(opt.count or 1):
                pending_tasks.append(asyncio.create_task(run_activity(i)))
            # Wait on them all and raise error if one happened
            done, _ = await workflow.wait(
                pending_tasks, return_when=asyncio.FIRST_EXCEPTION
            )
            for task in done:
                if task.exception():
                    raise task.exception()  # type: ignore
            # Otherwise return last result
            return True, last_result
        return False, None


async def cancel_after(task: asyncio.Task, after: float) -> None:
    await asyncio.sleep(after)
    task.cancel()


class ExternalWorker(ABC):
    """Worker guaranteed to have a "kitchen_sink" workflow."""

    @property
    @abstractmethod
    def task_queue(self) -> str:
        raise NotImplementedError

    @abstractmethod
    async def close(self):
        raise NotImplementedError


class ExternalPythonWorker(ExternalWorker):
    def __init__(self, env: WorkflowEnvironment) -> None:
        self.worker = Worker(
            env.client, task_queue=str(uuid.uuid4()), workflows=[KitchenSinkWorkflow]
        )
        self.run_task = asyncio.create_task(self.worker.run())

    @property
    def task_queue(self) -> str:
        return self.worker.task_queue

    async def close(self):
        await self.worker.shutdown()
        await self.run_task
