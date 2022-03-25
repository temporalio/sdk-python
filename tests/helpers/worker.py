"""Worker helper used for testing.

This invokes a "Kitchen Sink" workflow in an external worker. KS-prefixed class
names refer to types for the "Kitchen Sink" workflow.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from tests.helpers.golang import start_external_go_process


@dataclass
class KSWorkflowParams:
    actions: Optional[Iterable[KSAction]] = None
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
    args: Optional[Iterable[Any]] = None
    count: Optional[int] = None
    index_as_arg: Optional[bool] = None
    schedule_to_close_timeout_ms: Optional[int] = None
    start_to_close_timeout_ms: Optional[int] = None
    schedule_to_start_timeout_ms: Optional[int] = None
    cancel_after_ms: Optional[int] = None
    wait_for_cancellation: Optional[bool] = None
    heartbeat_timeout_ms: Optional[int] = None
    retry_max_attempts: Optional[int] = None
    non_retryable_error_types: Optional[Iterable[str]] = None


class ExternalWorker(ABC):
    """Worker guaranteed to have a "kitchen_sink" workflow."""

    @property
    @abstractmethod
    def task_queue(self) -> str:
        raise NotImplementedError

    @abstractmethod
    async def close(self):
        raise NotImplementedError


class ExternalGolangWorker(ExternalWorker):
    @staticmethod
    async def start(host_port: str, namespace: str) -> ExternalGolangWorker:
        task_queue = str(uuid.uuid4())
        process = await start_external_go_process(
            os.path.join(os.path.dirname(__file__), "golangworker"),
            "golangworker",
            host_port,
            namespace,
            task_queue,
        )
        return ExternalGolangWorker(task_queue, process)

    def __init__(self, task_queue: str, process: asyncio.subprocess.Process) -> None:
        super().__init__()
        self._task_queue = task_queue
        self._process = process

    @property
    def task_queue(self) -> str:
        return self._task_queue

    async def close(self):
        self._process.terminate()
        await self._process.wait()
