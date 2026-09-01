import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from strands import SandboxPathNotFoundError, SandboxTimeoutError, tool
from strands.sandbox import ExecutionResult, FileInfo, OutputFile, Sandbox, StreamChunk

import temporalio.contrib.strands._sandbox_activity
import temporalio.exceptions
import temporalio.testing
from temporalio import workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.contrib.strands import (
    SandboxWorkflowChain,
    SandboxWorkflowContext,
    StrandsPlugin,
    TemporalAgent,
    TemporalSandbox,
)
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.worker import Replayer, Worker
from tests.contrib.strands.common import get_activities


class RecordingSandbox(Sandbox):
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.files = {"/binary": b"\x00\xff"}

    async def execute_streaming(
        self,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamChunk | ExecutionResult, None]:
        self.calls.append(("execute", command, timeout, cwd, env, kwargs))
        yield StreamChunk("out")
        yield StreamChunk("err", "stderr")
        yield ExecutionResult(
            0,
            "out",
            "err",
            [OutputFile("artifact.bin", b"\x80\xff")],
        )

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
        self.calls.append(("execute_code", code, language, timeout, cwd, env, kwargs))
        yield ExecutionResult(0, code, "")

    async def read_file(self, path: str, **kwargs: Any) -> bytes:
        self.calls.append(("read_file", path, kwargs))
        return self.files[path]

    async def write_file(self, path: str, content: bytes, **kwargs: Any) -> None:
        self.calls.append(("write_file", path, content, kwargs))
        self.files[path] = content

    async def remove_file(self, path: str, **kwargs: Any) -> None:
        self.calls.append(("remove_file", path, kwargs))
        del self.files[path]

    async def list_files(self, path: str, **kwargs: Any) -> list[FileInfo]:
        self.calls.append(("list_files", path, kwargs))
        return [FileInfo("binary", False, len(self.files["/binary"]))]


@dataclass
class SandboxWorkflowResult:
    command_items_match: bool
    code_result: ExecutionResult
    binary_values_match: bool
    files: list[FileInfo]


@workflow.defn
class SandboxWorkflow:
    @workflow.run
    async def run(self) -> SandboxWorkflowResult:
        sandbox = TemporalSandbox(
            "recording", start_to_close_timeout=timedelta(seconds=15)
        )
        command_items = [
            item
            async for item in sandbox.execute_streaming(
                "echo hi",
                timeout=2,
                cwd="/work",
                env={"VISIBLE": "history"},
                future_option=True,
            )
        ]
        expected_command_items = [
            StreamChunk("out"),
            StreamChunk("err", "stderr"),
            ExecutionResult(
                0,
                "out",
                "err",
                [OutputFile("artifact.bin", b"\x80\xff")],
            ),
        ]
        code_result = await sandbox.execute_code(
            "print('hi')", "python3", future_option=2
        )
        original = await sandbox.read_file("/binary", future_option=3)
        await sandbox.write_file("/other", b"\x01\xfe", future_option=4)
        written = await sandbox.read_file("/other")
        await sandbox.remove_file("/other", future_option=5)
        files = await sandbox.list_files("/", future_option=6)
        return SandboxWorkflowResult(
            command_items == expected_command_items,
            code_result,
            original == b"\x00\xff" and written == b"\x01\xfe",
            files,
        )


async def test_sandbox_operations_are_durable_and_cached(client: Client):
    task_queue = f"test_sandbox-{uuid4()}"
    constructed: list[RecordingSandbox] = []
    contexts: list[SandboxWorkflowContext] = []

    def factory(context: SandboxWorkflowContext) -> RecordingSandbox:
        sandbox = RecordingSandbox()
        contexts.append(context)
        constructed.append(sandbox)
        return sandbox

    plugin = StrandsPlugin(models={}, sandboxes={"recording": factory})
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[SandboxWorkflow],
        plugins=[plugin],
        max_cached_workflows=0,
    ):
        handle = await client.start_workflow(
            SandboxWorkflow.run,
            id=f"test_sandbox-{uuid4()}",
            task_queue=task_queue,
        )
        result = await handle.result()

    assert result.command_items_match
    assert result.code_result == ExecutionResult(0, "print('hi')", "")
    assert result.binary_values_match
    assert result.files == [FileInfo("binary", False, 2)]
    assert len(constructed) == 1
    assert contexts == [
        SandboxWorkflowContext(
            chain=SandboxWorkflowChain(
                namespace=client.namespace,
                workflow_id=handle.id,
                first_execution_run_id=handle.first_execution_run_id or "",
            ),
            run_id=handle.result_run_id or "",
        )
    ]
    assert constructed[0].calls == [
        (
            "execute",
            "echo hi",
            2,
            "/work",
            {"VISIBLE": "history"},
            {"future_option": True},
        ),
        (
            "execute_code",
            "print('hi')",
            "python3",
            None,
            None,
            None,
            {"future_option": 2},
        ),
        ("read_file", "/binary", {"future_option": 3}),
        ("write_file", "/other", b"\x01\xfe", {"future_option": 4}),
        ("read_file", "/other", {}),
        ("remove_file", "/other", {"future_option": 5}),
        ("list_files", "/", {"future_option": 6}),
    ]

    history = await handle.fetch_history()
    assert get_activities(history) == [
        "strands-sandbox-execute",
        "strands-sandbox-execute-code",
        "strands-sandbox-read-file",
        "strands-sandbox-write-file",
        "strands-sandbox-read-file",
        "strands-sandbox-remove-file",
        "strands-sandbox-list-files",
    ]
    await Replayer(workflows=[SandboxWorkflow], plugins=[plugin]).replay_workflow(
        history
    )


@workflow.defn
class IsolatedSandboxWorkflow:
    @workflow.run
    async def run(self, value: bytes) -> bytes:
        sandbox = TemporalSandbox(
            "recording", start_to_close_timeout=timedelta(seconds=15)
        )
        await sandbox.write_file("/value", value)
        return await sandbox.read_file("/value")


async def test_sandbox_isolated_per_workflow_with_async_factory(client: Client):
    task_queue = f"test_sandbox_isolation-{uuid4()}"
    sandboxes: dict[SandboxWorkflowContext, RecordingSandbox] = {}

    async def factory(context: SandboxWorkflowContext) -> RecordingSandbox:
        await asyncio.sleep(0)
        sandbox = RecordingSandbox()
        sandboxes[context] = sandbox
        return sandbox

    plugin = StrandsPlugin(models={}, sandboxes={"recording": factory})
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[IsolatedSandboxWorkflow],
        plugins=[plugin],
        max_cached_workflows=0,
    ):
        handles = [
            await client.start_workflow(
                IsolatedSandboxWorkflow.run,
                value,
                id=f"test_sandbox_isolation-{uuid4()}",
                task_queue=task_queue,
            )
            for value in (b"first", b"second")
        ]
        assert await asyncio.gather(*(handle.result() for handle in handles)) == [
            b"first",
            b"second",
        ]

    assert len(sandboxes) == 2
    assert {context.workflow_id for context in sandboxes} == {
        handle.id for handle in handles
    }
    assert {sandbox.files["/value"] for sandbox in sandboxes.values()} == {
        b"first",
        b"second",
    }


@workflow.defn
class ContinueAsNewSandboxWorkflow:
    @workflow.run
    async def run(self, continued: bool = False) -> bytes:
        sandbox = TemporalSandbox(
            "recording", start_to_close_timeout=timedelta(seconds=15)
        )
        if not continued:
            await sandbox.write_file("/continued", b"same sandbox")
            workflow.continue_as_new(True)
        return await sandbox.read_file("/continued")


async def test_sandbox_reused_across_continue_as_new(client: Client):
    task_queue = f"test_sandbox_continue_as_new-{uuid4()}"
    contexts: list[SandboxWorkflowContext] = []

    def factory(context: SandboxWorkflowContext) -> RecordingSandbox:
        contexts.append(context)
        return RecordingSandbox()

    plugin = StrandsPlugin(models={}, sandboxes={"recording": factory})
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[ContinueAsNewSandboxWorkflow],
        plugins=[plugin],
        max_cached_workflows=0,
    ):
        handle = await client.start_workflow(
            ContinueAsNewSandboxWorkflow.run,
            id=f"test_sandbox_continue_as_new-{uuid4()}",
            task_queue=task_queue,
        )
        assert await handle.result() == b"same sandbox"

    assert len(contexts) == 1
    assert contexts[0].first_execution_run_id == handle.first_execution_run_id
    assert contexts[0].run_id == handle.first_execution_run_id


@workflow.defn
class RetriedSandboxWorkflow:
    @workflow.run
    async def run(self) -> bytes:
        sandbox = TemporalSandbox(
            "recording", start_to_close_timeout=timedelta(seconds=15)
        )
        if workflow.info().attempt == 1:
            await sandbox.write_file("/retried", b"same sandbox")
            raise RuntimeError("retry workflow")
        return await sandbox.read_file("/retried")


async def test_sandbox_reused_across_workflow_retry(client: Client):
    task_queue = f"test_sandbox_workflow_retry-{uuid4()}"
    contexts: list[SandboxWorkflowContext] = []

    def factory(context: SandboxWorkflowContext) -> RecordingSandbox:
        contexts.append(context)
        return RecordingSandbox()

    plugin = StrandsPlugin(models={}, sandboxes={"recording": factory})
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[RetriedSandboxWorkflow],
        plugins=[plugin],
        max_cached_workflows=0,
        workflow_failure_exception_types=[RuntimeError],
    ):
        handle = await client.start_workflow(
            RetriedSandboxWorkflow.run,
            id=f"test_sandbox_workflow_retry-{uuid4()}",
            task_queue=task_queue,
            retry_policy=RetryPolicy(
                initial_interval=timedelta(milliseconds=1), maximum_attempts=2
            ),
        )
        assert await handle.result() == b"same sandbox"

    assert len(contexts) == 1
    assert contexts[0].first_execution_run_id == handle.first_execution_run_id
    assert contexts[0].run_id == handle.first_execution_run_id


@workflow.defn
class IdleSandboxWorkflow:
    @workflow.run
    async def run(self) -> None:
        sandbox = TemporalSandbox(
            "recording", start_to_close_timeout=timedelta(seconds=15)
        )
        await sandbox.read_file("/binary")
        await workflow.sleep(0.25)
        await sandbox.read_file("/binary")


async def test_sandbox_cache_evicts_when_idle(client: Client):
    task_queue = f"test_sandbox_idle-{uuid4()}"
    contexts: list[SandboxWorkflowContext] = []

    def factory(context: SandboxWorkflowContext) -> RecordingSandbox:
        contexts.append(context)
        return RecordingSandbox()

    plugin = StrandsPlugin(
        models={},
        sandboxes={"recording": factory},
        sandbox_cache_idle_timeout=timedelta(milliseconds=50),
    )
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[IdleSandboxWorkflow],
        plugins=[plugin],
        max_cached_workflows=0,
    ):
        await client.execute_workflow(
            IdleSandboxWorkflow.run,
            id=f"test_sandbox_idle-{uuid4()}",
            task_queue=task_queue,
        )

    assert len(contexts) == 2
    assert contexts[0] == contexts[1]


class SlowSandbox(RecordingSandbox):
    async def read_file(self, path: str, **kwargs: Any) -> bytes:
        await asyncio.sleep(0.15)
        return await super().read_file(path, **kwargs)


@workflow.defn
class ConcurrentSandboxWorkflow:
    @workflow.run
    async def run(self) -> list[bytes]:
        sandbox = TemporalSandbox(
            "recording", start_to_close_timeout=timedelta(seconds=15)
        )
        return list(
            await asyncio.gather(
                sandbox.read_file("/binary"), sandbox.read_file("/binary")
            )
        )


async def test_sandbox_factory_is_single_flight_and_not_evicted_in_use(
    client: Client,
):
    task_queue = f"test_sandbox_single_flight-{uuid4()}"
    contexts: list[SandboxWorkflowContext] = []

    async def factory(context: SandboxWorkflowContext) -> SlowSandbox:
        contexts.append(context)
        await asyncio.sleep(0.05)
        return SlowSandbox()

    plugin = StrandsPlugin(
        models={},
        sandboxes={"recording": factory},
        sandbox_cache_idle_timeout=timedelta(milliseconds=25),
    )
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[ConcurrentSandboxWorkflow],
        plugins=[plugin],
        max_cached_workflows=0,
    ):
        result = await client.execute_workflow(
            ConcurrentSandboxWorkflow.run,
            id=f"test_sandbox_single_flight-{uuid4()}",
            task_queue=task_queue,
        )

    assert result == [b"\x00\xff", b"\x00\xff"]
    assert len(contexts) == 1


async def test_cancelled_waiter_preserves_completed_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory_calls = 0

    def factory(_: SandboxWorkflowContext) -> RecordingSandbox:
        nonlocal factory_calls
        factory_calls += 1
        return RecordingSandbox()

    sandbox_activities = temporalio.contrib.strands._sandbox_activity.SandboxActivities(
        {"recording": factory}
    )
    read_file = sandbox_activities.activities()[2]
    input = temporalio.contrib.strands._sandbox_activity._PathInput(
        "/binary",
        sandbox_name="recording",
        first_execution_run_id="first-run",
    )
    activity_environment = temporalio.testing.ActivityEnvironment()
    original_sandbox = (
        temporalio.contrib.strands._sandbox_activity._SandboxRecord.sandbox
    )

    async def cancel_after_creation(
        record: temporalio.contrib.strands._sandbox_activity._SandboxRecord,
    ) -> Sandbox:
        await original_sandbox(record)
        raise asyncio.CancelledError

    with monkeypatch.context() as patch:
        patch.setattr(
            temporalio.contrib.strands._sandbox_activity._SandboxRecord,
            "sandbox",
            cancel_after_creation,
        )
        with pytest.raises(asyncio.CancelledError):
            await activity_environment.run(read_file, input)

    assert await activity_environment.run(read_file, input) == b"\x00\xff"
    assert factory_calls == 1
    await sandbox_activities.aclose()


@pytest.mark.parametrize(
    ("activity_index", "input"),
    [
        (
            2,
            temporalio.contrib.strands._sandbox_activity._PathInput("/missing"),
        ),
        (
            3,
            temporalio.contrib.strands._sandbox_activity._WriteFileInput(
                "/missing", content_base64=""
            ),
        ),
        (
            4,
            temporalio.contrib.strands._sandbox_activity._PathInput("/missing"),
        ),
        (
            5,
            temporalio.contrib.strands._sandbox_activity._PathInput("/missing"),
        ),
    ],
    ids=["read", "write", "remove", "list"],
)
async def test_file_not_found_from_factory_remains_retryable(
    activity_index: int,
    input: temporalio.contrib.strands._sandbox_activity._WorkflowScopedInput,
) -> None:
    input.sandbox_name = "recording"
    input.first_execution_run_id = "first-run"

    def factory(_: SandboxWorkflowContext) -> Sandbox:
        raise FileNotFoundError("transient factory failure")

    sandbox_activities = temporalio.contrib.strands._sandbox_activity.SandboxActivities(
        {"recording": factory}
    )
    activity_environment = temporalio.testing.ActivityEnvironment()

    with pytest.raises(FileNotFoundError, match="transient factory failure"):
        await activity_environment.run(
            sandbox_activities.activities()[activity_index], input
        )


async def test_unknown_sandbox_name_is_retryable() -> None:
    sandbox_activities = temporalio.contrib.strands._sandbox_activity.SandboxActivities(
        {}
    )
    input = temporalio.contrib.strands._sandbox_activity._PathInput(
        "/missing",
        sandbox_name="missing",
        first_execution_run_id="first-run",
    )

    with pytest.raises(temporalio.exceptions.ApplicationError) as err:
        await temporalio.testing.ActivityEnvironment().run(
            sandbox_activities.activities()[2], input
        )

    assert (
        err.value.type
        == temporalio.contrib.strands._sandbox_activity.SANDBOX_NOT_FOUND_ERROR_TYPE
    )
    assert not err.value.non_retryable


async def test_shared_sandbox_cache_closes_after_last_run_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_calls = 0

    async def aclose(
        _: temporalio.contrib.strands._sandbox_activity.SandboxActivities,
    ) -> None:
        nonlocal close_calls
        close_calls += 1

    monkeypatch.setattr(
        temporalio.contrib.strands._sandbox_activity.SandboxActivities,
        "aclose",
        aclose,
    )
    plugin = StrandsPlugin(
        models={}, sandboxes={"recording": lambda _: RecordingSandbox()}
    )
    assert plugin.run_context is not None

    async with plugin.run_context():
        async with plugin.run_context():
            pass
        assert close_calls == 0

    assert close_calls == 1


def test_sandbox_cache_idle_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        StrandsPlugin(
            models={},
            sandboxes={"recording": lambda _: RecordingSandbox()},
            sandbox_cache_idle_timeout=timedelta(0),
        )


def test_sandbox_factories_share_one_activity_set() -> None:
    plugin = StrandsPlugin(
        models={},
        sandboxes={
            "first": lambda _: RecordingSandbox(),
            "second": lambda _: RecordingSandbox(),
        },
    )

    assert plugin.activities is not None
    assert not callable(plugin.activities)
    assert len(plugin.activities) == 6


def test_sandbox_workflow_context_separates_run_and_chain_identity() -> None:
    chain = SandboxWorkflowChain("namespace", "workflow", "first-run")
    first = SandboxWorkflowContext(chain, "run-1")
    second = SandboxWorkflowContext(chain, "run-2")

    assert first != second
    assert first.chain == second.chain
    assert first.namespace == "namespace"
    assert first.workflow_id == "workflow"
    assert first.first_execution_run_id == "first-run"


@tool(name="sandbox_bash")
def custom_bash(command: str) -> str:
    return command


def test_sandbox_default_tools_and_override() -> None:
    default_agent = TemporalAgent(
        model="mock",
        sandbox=TemporalSandbox("recording"),
        start_to_close_timeout=timedelta(seconds=15),
    )
    assert "sandbox_bash" in default_agent.tool_registry.registry
    assert "sandbox_file_editor" in default_agent.tool_registry.registry

    override_agent = TemporalAgent(
        model="mock",
        sandbox=TemporalSandbox("recording"),
        tools=[custom_bash],
        start_to_close_timeout=timedelta(seconds=15),
    )
    assert override_agent.tool_registry.registry["sandbox_bash"] is custom_bash
    assert "sandbox_file_editor" in override_agent.tool_registry.registry


@workflow.defn
class StreamingSandboxWorkflow:
    def __init__(self) -> None:
        self.stream = WorkflowStream()

    @workflow.run
    async def run(self) -> bool:
        sandbox = TemporalSandbox(
            "recording",
            start_to_close_timeout=timedelta(seconds=15),
            streaming_topic="sandbox-events",
        )
        result = [item async for item in sandbox.execute_streaming("echo hi")]
        return result[-1] == ExecutionResult(
            0,
            "out",
            "err",
            [OutputFile("artifact.bin", b"\x80\xff")],
        )


async def test_sandbox_streaming_publishes_raw_chunks(client: Client):
    task_queue = f"test_sandbox_streaming-{uuid4()}"
    workflow_id = f"test_sandbox_streaming-{uuid4()}"
    plugin = StrandsPlugin(
        models={}, sandboxes={"recording": lambda _: RecordingSandbox()}
    )
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[StreamingSandboxWorkflow],
        plugins=[plugin],
        max_cached_workflows=0,
    ):
        handle = await client.start_workflow(
            StreamingSandboxWorkflow.run,
            id=workflow_id,
            task_queue=task_queue,
        )
        stream = WorkflowStreamClient.create(client, workflow_id)
        events: list[StreamChunk] = []

        async def collect() -> None:
            async for stream_item in stream.subscribe(
                ["sandbox-events"],
                result_type=StreamChunk,
                poll_cooldown=timedelta(milliseconds=50),
            ):
                events.append(stream_item.data)
                if len(events) == 2:
                    break

        collect_task = asyncio.create_task(collect())
        assert await handle.result()
        await asyncio.wait_for(collect_task, timeout=10)

    assert events == [
        StreamChunk("out"),
        StreamChunk("err", "stderr"),
    ]
    await Replayer(
        workflows=[StreamingSandboxWorkflow], plugins=[plugin]
    ).replay_workflow(await handle.fetch_history())


class ErrorSandbox(RecordingSandbox):
    def __init__(self, *, always_timeout: bool = False) -> None:
        super().__init__()
        self.attempts = 0
        self.always_timeout = always_timeout

    async def execute_streaming(
        self,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamChunk | ExecutionResult, None]:
        self.attempts += 1
        if self.always_timeout:
            # The backing sandbox enforces its own limit, not the requested one.
            raise SandboxTimeoutError(90)
        if self.attempts == 1:
            raise RuntimeError("transient")
        yield ExecutionResult(0, "retried", "")

    async def read_file(self, path: str, **kwargs: Any) -> bytes:
        raise FileNotFoundError(f"cat: {path}: No such file or directory")

    async def list_files(self, path: str, **kwargs: Any) -> list[FileInfo]:
        raise SandboxPathNotFoundError(path)


@workflow.defn
class SandboxErrorWorkflow:
    @workflow.run
    async def run(self) -> tuple[str, bool, bool, str, str]:
        retried = TemporalSandbox(
            "retried",
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(milliseconds=1), maximum_attempts=3
            ),
        )
        result = await retried.execute("command", timeout=3)
        try:
            await retried.list_files("/missing")
        except SandboxPathNotFoundError:
            path_error = True
        else:
            path_error = False

        # A plain FileNotFoundError from the sandbox arrives as the sandbox
        # subclass, keeping the backing environment's own message.
        try:
            await retried.read_file("/missing")
        except SandboxPathNotFoundError as err:
            read_message = str(err)
        else:
            read_message = ""

        # No retry policy: a timeout must surface on the first attempt rather
        # than retrying under Temporal's unlimited-attempt default. The
        # schedule-to-close timeout bounds the failure if that ever regresses.
        failing = TemporalSandbox(
            "failing",
            start_to_close_timeout=timedelta(seconds=5),
            schedule_to_close_timeout=timedelta(seconds=15),
        )
        try:
            await failing.execute("command", timeout=4)
        except SandboxTimeoutError as err:
            timeout_error = True
            timeout_message = str(err)
        else:
            timeout_error = False
            timeout_message = ""
        return result.stdout, path_error, timeout_error, read_message, timeout_message


async def test_sandbox_retries_and_reconstructs_errors(client: Client):
    task_queue = f"test_sandbox_errors-{uuid4()}"
    retried = ErrorSandbox()
    failing = ErrorSandbox(always_timeout=True)
    factory_attempts = 0

    def retried_factory(_: SandboxWorkflowContext) -> ErrorSandbox:
        nonlocal factory_attempts
        factory_attempts += 1
        if factory_attempts == 1:
            raise RuntimeError("transient factory failure")
        return retried

    plugin = StrandsPlugin(
        models={},
        sandboxes={"retried": retried_factory, "failing": lambda _: failing},
    )
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[SandboxErrorWorkflow],
        plugins=[plugin],
        max_cached_workflows=0,
    ):
        result = await client.execute_workflow(
            SandboxErrorWorkflow.run,
            id=f"test_sandbox_errors-{uuid4()}",
            task_queue=task_queue,
        )

    assert result == (
        "retried",
        True,
        True,
        "cat: /missing: No such file or directory",
        "Execution timed out after 90 seconds",
    )
    assert factory_attempts == 2
    assert retried.attempts == 2
    assert failing.attempts == 1
