from __future__ import annotations

import asyncio
import base64
import inspect
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from strands.sandbox import (
    ExecutionResult,
    FileInfo,
    Sandbox,
    StreamChunk,
)
from strands.sandbox.errors import SandboxTimeoutError

from temporalio import activity
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.exceptions import ApplicationError

from ._heartbeat_decorator import auto_heartbeater

SANDBOX_TIMEOUT_ERROR_TYPE = "StrandsSandboxTimeoutError"
SANDBOX_PATH_NOT_FOUND_ERROR_TYPE = "StrandsSandboxPathNotFoundError"
SANDBOX_NOT_FOUND_ERROR_TYPE = "StrandsSandboxNotFoundError"
_SANDBOX_CACHE_IDLE_TIMEOUT = timedelta(minutes=5)


@dataclass(frozen=True)
class SandboxWorkflowChain:
    """Identity shared by every run in a Workflow chain."""

    namespace: str
    workflow_id: str
    first_execution_run_id: str


@dataclass(frozen=True)
class SandboxWorkflowContext:
    """Workflow execution requesting a worker-side sandbox."""

    chain: SandboxWorkflowChain
    run_id: str

    @property
    def namespace(self) -> str:
        """Namespace containing the Workflow."""
        return self.chain.namespace

    @property
    def workflow_id(self) -> str:
        """Workflow ID shared by the execution chain."""
        return self.chain.workflow_id

    @property
    def first_execution_run_id(self) -> str:
        """Run ID identifying the execution chain."""
        return self.chain.first_execution_run_id


SandboxFactory = Callable[[SandboxWorkflowContext], Sandbox | Awaitable[Sandbox]]
_SandboxKey = tuple[str, SandboxWorkflowChain]


@dataclass
class _WorkflowScopedInput:
    sandbox_name: str = field(default="", kw_only=True)
    first_execution_run_id: str = field(default="", kw_only=True)


@dataclass
class _ExecuteInput(_WorkflowScopedInput):
    command: str
    timeout: float | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None
    kwargs: dict[str, Any] = field(default_factory=dict)
    streaming_topic: str | None = None
    streaming_batch_interval_seconds: float = 0.1


@dataclass
class _ExecuteCodeInput(_WorkflowScopedInput):
    code: str
    language: str
    timeout: float | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None
    kwargs: dict[str, Any] = field(default_factory=dict)
    streaming_topic: str | None = None
    streaming_batch_interval_seconds: float = 0.1


@dataclass
class _PathInput(_WorkflowScopedInput):
    path: str
    kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class _WriteFileInput(_PathInput):
    content_base64: str = ""


@dataclass
class _StreamItem:
    value: dict[str, Any]


class _SandboxRecord:
    def __init__(
        self,
        owner: SandboxActivities,
        key: _SandboxKey,
        context: SandboxWorkflowContext,
        factory: SandboxFactory,
        idle_timeout: timedelta,
    ) -> None:
        self._owner = owner
        self._key = key
        self._context = context
        self._idle_timeout = idle_timeout
        self._inflight = 0
        self._idle_handle: asyncio.TimerHandle | None = None
        self._sandbox_task = asyncio.create_task(self._create(factory))

    async def _create(self, factory: SandboxFactory) -> Sandbox:
        sandbox = factory(self._context)
        if inspect.isawaitable(sandbox):
            return await sandbox
        return sandbox

    def acquire(self) -> None:
        self._inflight += 1
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None

    def release(self) -> None:
        self._inflight -= 1
        if self._inflight == 0 and self._owner._has_record(self._key, self):
            self._idle_handle = asyncio.get_running_loop().call_later(
                self._idle_timeout.total_seconds(), self._on_idle
            )

    def _on_idle(self) -> None:
        self._idle_handle = None
        if self._inflight == 0:
            self._owner._evict(self._key, self)

    async def sandbox(self) -> Sandbox:
        return await asyncio.shield(self._sandbox_task)

    def creation_failed(self) -> bool:
        return self._sandbox_task.done() and (
            self._sandbox_task.cancelled() or self._sandbox_task.exception() is not None
        )

    async def aclose(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        if not self._sandbox_task.done():
            self._sandbox_task.cancel()
        try:
            await self._sandbox_task
        except BaseException:
            pass


class SandboxActivities:
    """Lazily resolves Workflow-scoped sandboxes and exposes their activities."""

    def __init__(
        self,
        factories: dict[str, SandboxFactory],
        idle_timeout: timedelta | None = None,
    ) -> None:
        """Store named Workflow-scoped worker-side sandbox factories."""
        self._factories = dict(factories)
        self._idle_timeout = (
            idle_timeout if idle_timeout is not None else _SANDBOX_CACHE_IDLE_TIMEOUT
        )
        if self._idle_timeout <= timedelta(0):
            raise ValueError("Sandbox cache idle timeout must be positive")
        self._records: dict[_SandboxKey, _SandboxRecord] = {}

    @asynccontextmanager
    async def _sandbox(
        self, input: _WorkflowScopedInput
    ) -> AsyncGenerator[Sandbox, None]:
        info = activity.info()
        if (
            not info.workflow_id
            or not info.workflow_run_id
            or not input.first_execution_run_id
        ):
            raise RuntimeError("Sandbox activities must be started by a Workflow")
        context = SandboxWorkflowContext(
            chain=SandboxWorkflowChain(
                namespace=info.namespace,
                workflow_id=info.workflow_id,
                first_execution_run_id=input.first_execution_run_id,
            ),
            run_id=info.workflow_run_id,
        )
        factory = self._factories.get(input.sandbox_name)
        if factory is None:
            raise ApplicationError(
                f"Unknown sandbox name {input.sandbox_name!r}. "
                f"Known: {sorted(self._factories)}",
                type=SANDBOX_NOT_FOUND_ERROR_TYPE,
            )
        key = (input.sandbox_name, context.chain)
        record = self._records.get(key)
        if record is None:
            record = _SandboxRecord(self, key, context, factory, self._idle_timeout)
            self._records[key] = record
        record.acquire()
        try:
            try:
                sandbox = await record.sandbox()
            except asyncio.CancelledError:
                # A cancelled waiter must not discard a sandbox that another
                # activity for the same Workflow may already be using.
                if record.creation_failed():
                    self._evict(key, record)
                raise
            except BaseException:
                self._evict(key, record)
                raise
            yield sandbox
        finally:
            record.release()

    def _has_record(self, key: _SandboxKey, record: _SandboxRecord) -> bool:
        return self._records.get(key) is record

    def _evict(self, key: _SandboxKey, record: _SandboxRecord) -> None:
        if self._has_record(key, record):
            del self._records[key]

    async def aclose(self) -> None:
        """Cancel cache timers and discard all worker-local sandbox adapters."""
        records = list(self._records.values())
        self._records.clear()
        for record in records:
            await record.aclose()

    def activities(self) -> list[Callable[..., Any]]:
        """Build one stable activity set that dispatches by sandbox name."""

        @activity.defn(name=_activity_name("execute"))
        @auto_heartbeater
        async def execute(input: _ExecuteInput) -> list[_StreamItem]:
            async with self._sandbox(input) as sandbox:
                return await self._run_stream(
                    sandbox.execute_streaming(
                        input.command,
                        timeout=input.timeout,
                        cwd=input.cwd,
                        env=input.env,
                        **input.kwargs,
                    ),
                    timeout=input.timeout,
                    streaming_topic=input.streaming_topic,
                    streaming_batch_interval_seconds=input.streaming_batch_interval_seconds,
                )

        @activity.defn(name=_activity_name("execute-code"))
        @auto_heartbeater
        async def execute_code(
            input: _ExecuteCodeInput,
        ) -> list[_StreamItem]:
            async with self._sandbox(input) as sandbox:
                return await self._run_stream(
                    sandbox.execute_code_streaming(
                        input.code,
                        input.language,
                        timeout=input.timeout,
                        cwd=input.cwd,
                        env=input.env,
                        **input.kwargs,
                    ),
                    timeout=input.timeout,
                    streaming_topic=input.streaming_topic,
                    streaming_batch_interval_seconds=input.streaming_batch_interval_seconds,
                )

        @activity.defn(name=_activity_name("read-file"))
        @auto_heartbeater
        async def read_file(input: _PathInput) -> bytes:
            async with self._sandbox(input) as sandbox:
                try:
                    return await sandbox.read_file(input.path, **input.kwargs)
                except FileNotFoundError as err:
                    raise _path_not_found_error(err, input.path) from err

        @activity.defn(name=_activity_name("write-file"))
        @auto_heartbeater
        async def write_file(input: _WriteFileInput) -> None:
            async with self._sandbox(input) as sandbox:
                try:
                    await sandbox.write_file(
                        input.path,
                        base64.b64decode(input.content_base64),
                        **input.kwargs,
                    )
                except FileNotFoundError as err:
                    raise _path_not_found_error(err, input.path) from err

        @activity.defn(name=_activity_name("remove-file"))
        @auto_heartbeater
        async def remove_file(input: _PathInput) -> None:
            async with self._sandbox(input) as sandbox:
                try:
                    await sandbox.remove_file(input.path, **input.kwargs)
                except FileNotFoundError as err:
                    raise _path_not_found_error(err, input.path) from err

        @activity.defn(name=_activity_name("list-files"))
        @auto_heartbeater
        async def list_files(input: _PathInput) -> list[FileInfo]:
            async with self._sandbox(input) as sandbox:
                try:
                    return await sandbox.list_files(input.path, **input.kwargs)
                except FileNotFoundError as err:
                    raise _path_not_found_error(err, input.path) from err

        return [execute, execute_code, read_file, write_file, remove_file, list_files]

    async def _run_stream(
        self,
        stream: AsyncGenerator[StreamChunk | ExecutionResult, None],
        *,
        timeout: float | None,
        streaming_topic: str | None,
        streaming_batch_interval_seconds: float,
    ) -> list[_StreamItem]:
        items: list[_StreamItem] = []
        try:
            if streaming_topic is None:
                async for item in stream:
                    items.append(_StreamItem(_item_to_json(item)))
                return items

            client = WorkflowStreamClient.from_within_activity(
                batch_interval=timedelta(seconds=streaming_batch_interval_seconds),
            )
            topic = client.topic(streaming_topic, type=StreamChunk)
            async with client:
                async for item in stream:
                    items.append(_StreamItem(_item_to_json(item)))
                    if isinstance(item, StreamChunk):
                        topic.publish(item)
            return items
        except SandboxTimeoutError as err:
            raise _timeout_error(err, timeout) from err


def _activity_name(operation: str) -> str:
    return f"strands-sandbox-{operation}"


def _timeout_error(err: SandboxTimeoutError, timeout: float | None) -> ApplicationError:
    # A timeout is the deterministic outcome the caller asked for, so retrying
    # just repeats it. Surface it to workflow code on the first attempt instead.
    return ApplicationError(
        str(err),
        timeout,
        type=SANDBOX_TIMEOUT_ERROR_TYPE,
        non_retryable=True,
    )


def _path_not_found_error(err: FileNotFoundError, path: str) -> ApplicationError:
    # Strands documents FileNotFoundError, not SandboxPathNotFoundError, for
    # read/remove/list; only list_files raises the sandbox-specific subclass.
    # Either way the path is missing on every attempt, so retrying is futile.
    return ApplicationError(
        str(err),
        path,
        type=SANDBOX_PATH_NOT_FOUND_ERROR_TYPE,
        non_retryable=True,
    )


def _item_to_json(item: StreamChunk | ExecutionResult) -> dict[str, Any]:
    if isinstance(item, StreamChunk):
        return {
            "kind": "stream_chunk",
            "data": item.data,
            "stream_type": item.stream_type,
        }
    return {
        "kind": "execution_result",
        "exit_code": item.exit_code,
        "stdout": item.stdout,
        "stderr": item.stderr,
        "output_files": [
            {
                "name": output.name,
                "content_base64": base64.b64encode(output.content).decode("ascii"),
                "mime_type": output.mime_type,
            }
            for output in item.output_files
        ],
    }
