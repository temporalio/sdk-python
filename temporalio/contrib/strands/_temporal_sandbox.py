import base64
from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import Any, TypeVar

from strands.sandbox import ExecutionResult, FileInfo, OutputFile, Sandbox, StreamChunk
from strands.sandbox.errors import SandboxPathNotFoundError, SandboxTimeoutError
from strands.types.tools import AgentTool
from strands.vended_tools.bash import make_bash
from strands.vended_tools.file_editor import make_file_editor

from temporalio import workflow
from temporalio.common import Priority, RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.workflow import ActivityCancellationType, VersioningIntent

from ._sandbox_activity import (
    SANDBOX_PATH_NOT_FOUND_ERROR_TYPE,
    SANDBOX_TIMEOUT_ERROR_TYPE,
    _activity_name,
    _ExecuteCodeInput,
    _ExecuteInput,
    _PathInput,
    _StreamItem,
    _WriteFileInput,
)

_ErrorT = TypeVar("_ErrorT", bound=OSError)


class TemporalSandbox(Sandbox):
    """Workflow-side sandbox that dispatches operations as Temporal activities."""

    def __init__(
        self,
        name: str,
        *,
        task_queue: str | None = None,
        schedule_to_close_timeout: timedelta | None = None,
        schedule_to_start_timeout: timedelta | None = None,
        start_to_close_timeout: timedelta | None = None,
        heartbeat_timeout: timedelta | None = None,
        retry_policy: RetryPolicy | None = None,
        cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
        versioning_intent: VersioningIntent | None = None,
        summary: str | None = None,
        priority: Priority = Priority.default,
        streaming_topic: str | None = None,
        streaming_batch_interval: timedelta = timedelta(milliseconds=100),
    ) -> None:
        """Configure a registered sandbox name and its activity options."""
        self._name = name
        self._streaming_topic = streaming_topic
        self._streaming_batch_interval = streaming_batch_interval
        self._options: dict[str, Any] = {
            "task_queue": task_queue,
            "schedule_to_close_timeout": schedule_to_close_timeout,
            "schedule_to_start_timeout": schedule_to_start_timeout,
            "start_to_close_timeout": start_to_close_timeout,
            "heartbeat_timeout": heartbeat_timeout,
            "retry_policy": retry_policy,
            "cancellation_type": cancellation_type,
            "versioning_intent": versioning_intent,
            "summary": summary,
            "priority": priority,
        }

    async def execute_streaming(
        self,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamChunk | ExecutionResult, None]:
        """Execute a command in the registered worker-side sandbox."""
        items = await self._execute(
            "execute",
            _ExecuteInput(
                command=command,
                timeout=timeout,
                cwd=cwd,
                env=env,
                kwargs=kwargs,
                streaming_topic=self._streaming_topic,
                streaming_batch_interval_seconds=self._streaming_batch_interval.total_seconds(),
            ),
            result_type=list[_StreamItem],
        )
        for item in items:
            yield _item_from_json(item.value)

    async def execute_code_streaming(
        self,
        code: str,
        language: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamChunk | ExecutionResult, None]:
        """Execute code in the registered worker-side sandbox."""
        items = await self._execute(
            "execute-code",
            _ExecuteCodeInput(
                code=code,
                language=language,
                timeout=timeout,
                cwd=cwd,
                env=env,
                kwargs=kwargs,
                streaming_topic=self._streaming_topic,
                streaming_batch_interval_seconds=self._streaming_batch_interval.total_seconds(),
            ),
            result_type=list[_StreamItem],
        )
        for item in items:
            yield _item_from_json(item.value)

    async def read_file(self, path: str, **kwargs: Any) -> bytes:
        """Read bytes from the registered worker-side sandbox."""
        return await self._execute(
            "read-file", _PathInput(path, kwargs), result_type=bytes
        )

    async def write_file(self, path: str, content: bytes, **kwargs: Any) -> None:
        """Write bytes to the registered worker-side sandbox."""
        await self._execute(
            "write-file",
            _WriteFileInput(path, kwargs, base64.b64encode(content).decode("ascii")),
        )

    async def remove_file(self, path: str, **kwargs: Any) -> None:
        """Remove a file from the registered worker-side sandbox."""
        await self._execute("remove-file", _PathInput(path, kwargs))

    async def list_files(self, path: str, **kwargs: Any) -> list[FileInfo]:
        """List a directory in the registered worker-side sandbox."""
        return await self._execute(
            "list-files", _PathInput(path, kwargs), result_type=list[FileInfo]
        )

    def get_tools(self) -> list[AgentTool]:
        """Vend Strands' standard bash and file-editor sandbox tools."""
        return [
            make_file_editor(sandbox=self, name="sandbox_file_editor"),
            make_bash(sandbox=self, name="sandbox_bash"),
        ]

    async def _execute(
        self, operation: str, input: Any, *, result_type: type | None = None
    ) -> Any:
        input.sandbox_name = self._name
        input.first_execution_run_id = workflow.info().first_execution_run_id
        try:
            return await workflow.execute_activity(
                _activity_name(operation),
                input,
                result_type=result_type,
                **self._options,
            )
        except ActivityError as err:
            cause = err.__cause__
            if isinstance(cause, ApplicationError):
                if cause.type == SANDBOX_TIMEOUT_ERROR_TYPE:
                    seconds = cause.details[0] if cause.details else None
                    raise _with_message(SandboxTimeoutError(seconds), cause) from err
                if cause.type == SANDBOX_PATH_NOT_FOUND_ERROR_TYPE:
                    path = cause.details[0] if cause.details else ""
                    raise _with_message(SandboxPathNotFoundError(path), cause) from err
            raise


def _with_message(error: _ErrorT, cause: ApplicationError) -> _ErrorT:
    # The details only carry what the workflow needs to rebuild the error type.
    # Restore the sandbox's own message so a timeout reports the duration the
    # sandbox actually enforced, not the one the caller requested, and a missing
    # path keeps whatever the backing environment said about it.
    if cause.message:
        error.args = (cause.message,)
    return error


def _item_from_json(value: Any) -> StreamChunk | ExecutionResult:
    if not isinstance(value, dict):
        raise TypeError("Sandbox stream item must be an object")
    if value.get("kind") == "stream_chunk":
        return StreamChunk(value["data"], value["stream_type"])
    if value.get("kind") == "execution_result":
        return ExecutionResult(
            exit_code=value["exit_code"],
            stdout=value["stdout"],
            stderr=value["stderr"],
            output_files=[
                OutputFile(
                    name=output["name"],
                    content=base64.b64decode(output["content_base64"]),
                    mime_type=output["mime_type"],
                )
                for output in value["output_files"]
            ],
        )
    raise ValueError(f"Unknown sandbox stream item kind: {value.get('kind')!r}")
