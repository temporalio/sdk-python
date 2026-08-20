import base64
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from strands.sandbox import (
    ExecutionResult,
    FileInfo,
    Sandbox,
    StreamChunk,
)
from strands.sandbox.errors import SandboxPathNotFoundError, SandboxTimeoutError

from temporalio import activity
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.exceptions import ApplicationError

from ._heartbeat_decorator import auto_heartbeater

SANDBOX_TIMEOUT_ERROR_TYPE = "StrandsSandboxTimeoutError"
SANDBOX_PATH_NOT_FOUND_ERROR_TYPE = "StrandsSandboxPathNotFoundError"


@dataclass
class _ExecuteInput:
    command: str
    timeout: float | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None
    kwargs: dict[str, Any] = field(default_factory=dict)
    streaming_topic: str | None = None
    streaming_batch_interval_seconds: float = 0.1


@dataclass
class _ExecuteCodeInput:
    code: str
    language: str
    timeout: float | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None
    kwargs: dict[str, Any] = field(default_factory=dict)
    streaming_topic: str | None = None
    streaming_batch_interval_seconds: float = 0.1


@dataclass
class _PathInput:
    path: str
    kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class _WriteFileInput(_PathInput):
    content_base64: str = ""


@dataclass
class _StreamItem:
    value: dict[str, Any]


class SandboxActivities:
    """Lazily resolves one registered sandbox and exposes its activities."""

    def __init__(self, name: str, factory: Callable[[], Sandbox]) -> None:
        """Store a sandbox name and its lazy worker-side factory."""
        self._name = name
        self._factory = factory
        self._sandbox: Sandbox | None = None

    def _get_sandbox(self) -> Sandbox:
        if self._sandbox is None:
            self._sandbox = self._factory()
        return self._sandbox

    def activities(self) -> list[Callable[..., Any]]:
        """Build stable, name-prefixed activities for this sandbox."""

        @activity.defn(name=_activity_name(self._name, "execute"))
        @auto_heartbeater
        async def execute(input: _ExecuteInput) -> list[_StreamItem]:
            return await self._run_stream(
                self._get_sandbox().execute_streaming(
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

        @activity.defn(name=_activity_name(self._name, "execute-code"))
        @auto_heartbeater
        async def execute_code(
            input: _ExecuteCodeInput,
        ) -> list[_StreamItem]:
            return await self._run_stream(
                self._get_sandbox().execute_code_streaming(
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

        @activity.defn(name=_activity_name(self._name, "read-file"))
        @auto_heartbeater
        async def read_file(input: _PathInput) -> bytes:
            try:
                return await self._get_sandbox().read_file(input.path, **input.kwargs)
            except SandboxPathNotFoundError as err:
                raise _path_not_found_error(err, input.path) from err

        @activity.defn(name=_activity_name(self._name, "write-file"))
        @auto_heartbeater
        async def write_file(input: _WriteFileInput) -> None:
            try:
                await self._get_sandbox().write_file(
                    input.path,
                    base64.b64decode(input.content_base64),
                    **input.kwargs,
                )
            except SandboxPathNotFoundError as err:
                raise _path_not_found_error(err, input.path) from err

        @activity.defn(name=_activity_name(self._name, "remove-file"))
        @auto_heartbeater
        async def remove_file(input: _PathInput) -> None:
            try:
                await self._get_sandbox().remove_file(input.path, **input.kwargs)
            except SandboxPathNotFoundError as err:
                raise _path_not_found_error(err, input.path) from err

        @activity.defn(name=_activity_name(self._name, "list-files"))
        @auto_heartbeater
        async def list_files(input: _PathInput) -> list[FileInfo]:
            try:
                return await self._get_sandbox().list_files(input.path, **input.kwargs)
            except SandboxPathNotFoundError as err:
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


def _activity_name(sandbox_name: str, operation: str) -> str:
    return f"{sandbox_name}-sandbox-{operation}"


def _timeout_error(err: SandboxTimeoutError, timeout: float | None) -> ApplicationError:
    # A timeout is the deterministic outcome the caller asked for, so retrying
    # just repeats it. Surface it to workflow code on the first attempt instead.
    return ApplicationError(
        str(err),
        timeout,
        type=SANDBOX_TIMEOUT_ERROR_TYPE,
        non_retryable=True,
    )


def _path_not_found_error(err: SandboxPathNotFoundError, path: str) -> ApplicationError:
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
