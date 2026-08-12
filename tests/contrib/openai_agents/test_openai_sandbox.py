"""Tests for sandbox validation in TemporalOpenAIRunner."""

import io
import json
import sys
import uuid
from base64 import b64encode
from collections.abc import Callable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
from agents import Agent, FunctionTool, RunConfig, Runner, Tool
from agents.sandbox import Capability, Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.errors import (
    ExecTransportError,
    SandboxError,
    WorkspaceArchiveReadError,
)
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.sandbox_client import (
    BaseSandboxClient,
    BaseSandboxClientOptions,
)
from agents.sandbox.session.sandbox_session import SandboxSession
from agents.sandbox.session.sandbox_session_state import SandboxSessionState
from agents.sandbox.snapshot import NoopSnapshot
from agents.sandbox.types import ExecResult
from pydantic import TypeAdapter
from pydantic_core import to_json

from temporalio import workflow
from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import Client, WorkflowFailureError
from temporalio.contrib.openai_agents import (
    ModelActivityParameters,
    OpenAIAgentsPlugin,
    SandboxClientProvider,
)
from temporalio.contrib.openai_agents._openai_runner import _has_sandbox_agent
from temporalio.contrib.openai_agents.sandbox._temporal_activity_models import (
    CreateSessionArgs,
    ExecArgs,
    HydrateWorkspaceArgs,
    PersistWorkspaceResult,
    PtyExecUpdateResult,
    ReadArgs,
    ReadResult,
    ResumeSessionArgs,
    RunningArgs,
    StopArgs,
    WriteArgs,
)
from temporalio.contrib.openai_agents.sandbox._temporal_activity_models import (
    ExecResult as ExecResultModel,
)
from temporalio.contrib.openai_agents.sandbox._temporal_sandbox_client import (
    TemporalSandboxClient,
)
from temporalio.contrib.openai_agents.testing import (
    AgentEnvironment,
    ResponseBuilders,
    TestModel,
    TestModelProvider,
)
from temporalio.contrib.openai_agents.workflow import temporal_sandbox_client
from temporalio.exceptions import ApplicationError
from temporalio.workflow import ActivityConfig
from tests.helpers import new_worker

# ── _has_sandbox_agent unit tests ──


def test_has_sandbox_agent_regular_agent():
    assert _has_sandbox_agent(Agent[None](name="regular")) is False


def test_has_sandbox_agent_sandbox_starting():
    assert _has_sandbox_agent(SandboxAgent[None](name="sandbox")) is True


def test_has_sandbox_agent_sandbox_direct_handoff():
    sandbox = SandboxAgent[None](name="sandbox")
    regular = Agent[None](name="regular", handoffs=[sandbox])
    assert _has_sandbox_agent(regular) is True


def test_has_sandbox_agent_sandbox_deep_handoff():
    sandbox = SandboxAgent[None](name="sandbox")
    middle = Agent[None](name="middle", handoffs=[sandbox])
    top = Agent[None](name="top", handoffs=[middle])
    assert _has_sandbox_agent(top) is True


def test_has_sandbox_agent_no_sandbox_in_chain():
    c = Agent[None](name="c")
    b = Agent[None](name="b", handoffs=[c])
    a = Agent[None](name="a", handoffs=[b])
    assert _has_sandbox_agent(a) is False


def test_has_sandbox_agent_circular_no_sandbox():
    a: Agent[Any] = Agent[None](name="a")
    b: Agent[Any] = Agent[None](name="b", handoffs=[a])
    a.handoffs = [b]
    assert _has_sandbox_agent(a) is False


def test_has_sandbox_agent_circular_with_sandbox():
    sandbox = SandboxAgent[None](name="sandbox")
    a: Agent[Any] = Agent[None](name="a", handoffs=[sandbox])
    b: Agent[Any] = Agent[None](name="b", handoffs=[a])
    a.handoffs = [b, sandbox]
    assert _has_sandbox_agent(b) is True


# ── temporal_sandbox_client helper tests ──


def test_temporal_sandbox_client_returns_temporal_client():
    client = temporal_sandbox_client("my-backend")
    assert isinstance(client, TemporalSandboxClient)
    assert client._name == "my-backend"
    assert client.backend_id == "my-backend"


def test_temporal_sandbox_client_with_config():
    config = ActivityConfig(start_to_close_timeout=timedelta(minutes=10))
    client = temporal_sandbox_client("my-backend", config=config)
    assert isinstance(client, TemporalSandboxClient)
    assert client._config == config


# ── Workflow validation tests ──


def _mock_model():
    return TestModel.returning_responses([ResponseBuilders.output_message("test")])


@workflow.defn
class SandboxValidationWorkflow:
    """Single workflow that validates all sandbox configuration error cases."""

    @workflow.run
    async def run(self) -> str:
        # Case 1: SandboxAgent without run_config.sandbox
        try:
            agent = SandboxAgent[None](name="sandbox")
            await Runner.run(starting_agent=agent, input="hello")
            return "FAIL: no-config should have raised"
        except ValueError as e:
            assert "run_config.sandbox is not configured" in str(e)

        # Case 2: SandboxAgent reachable via handoff without run_config.sandbox
        try:
            sandbox = SandboxAgent[None](name="sandbox_target")
            router = Agent[None](name="router", handoffs=[sandbox])
            await Runner.run(starting_agent=router, input="hello")
            return "FAIL: handoff-no-config should have raised"
        except ValueError as e:
            assert "run_config.sandbox is not configured" in str(e)

        # Case 3: SandboxRunConfig with client=None
        try:
            agent = SandboxAgent[None](name="sandbox")
            await Runner.run(
                starting_agent=agent,
                input="hello",
                run_config=RunConfig(
                    sandbox=SandboxRunConfig(client=None),  # type: ignore[arg-type]
                ),
            )
            return "FAIL: null-client should have raised"
        except ValueError as e:
            assert "run_config.sandbox.client must be set" in str(e)

        # Case 4: Non-TemporalSandboxClient in run_config.sandbox.client
        try:
            agent = SandboxAgent[None](name="sandbox")
            await Runner.run(
                starting_agent=agent,
                input="hello",
                run_config=RunConfig(
                    sandbox=SandboxRunConfig(client=object()),  # type: ignore[arg-type]
                ),
            )
            return "FAIL: wrong-client should have raised"
        except ValueError as e:
            assert "temporal_sandbox_client(name)" in str(e)

        return "OK"


async def test_sandbox_validation_errors(client: Client):
    """All sandbox configuration errors should be caught immediately in the workflow."""
    async with AgentEnvironment(model=_mock_model()) as env:
        client = env.applied_on_client(client)
        async with new_worker(
            client,
            SandboxValidationWorkflow,
            workflow_failure_exception_types=[ValueError, AssertionError],
        ) as worker:
            result = await client.execute_workflow(
                SandboxValidationWorkflow.run,
                id=f"sandbox-validation-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=10),
            )
            assert result == "OK"


# ── Mock sandbox infrastructure for delegation tests ──


class TestSessionState(SandboxSessionState):
    """Concrete ``SandboxSessionState`` subclass for tests that don't need a real backend."""

    __test__ = False
    type: Literal["test"] = "test"  # type: ignore


class _MockSandboxSession(BaseSandboxSession):
    """Minimal mock session that tracks calls and returns canned results."""

    def __init__(self, manifest: Manifest | None = None) -> None:
        self.state = TestSessionState(
            manifest=manifest or Manifest(),
            snapshot=NoopSnapshot(id=str(uuid.uuid4())),
        )
        self.exec_calls: list[tuple] = []
        self.read_calls: list[Path] = []
        self.write_calls: list[tuple[Path, bytes]] = []
        self.running_calls: int = 0
        self.start_calls: int = 0
        self.stop_calls: int = 0
        self.shutdown_calls: int = 0
        self.persist_workspace_calls: int = 0
        self.hydrate_workspace_calls: int = 0

    async def start(self) -> None:
        self.start_calls += 1

    async def stop(self) -> None:
        self.stop_calls += 1

    async def shutdown(self) -> None:
        self.shutdown_calls += 1

    async def running(self) -> bool:
        self.running_calls += 1
        return True

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        self.exec_calls.append((command, timeout))
        return ExecResult(stdout=b"ok\n", stderr=b"", exit_code=0)

    async def read(self, path: Path, *, user: Any = None) -> io.IOBase:  # type: ignore[reportUnusedParameter]
        self.read_calls.append(path)
        return io.BytesIO(b"file-content")

    async def write(self, path: Path, data: io.IOBase, *, user: Any = None) -> None:  # type: ignore[reportUnusedParameter]
        self.write_calls.append((path, data.read()))

    async def persist_workspace(self) -> io.IOBase:
        self.persist_workspace_calls += 1
        return io.BytesIO(b"workspace-archive")

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        self.hydrate_workspace_calls += 1

    def supports_pty(self) -> bool:
        return False


class _MockSandboxClient(BaseSandboxClient[BaseSandboxClientOptions | None]):
    """Mock client that tracks create/resume/delete calls and delegates to a mock session."""

    backend_id = "mock"
    supports_default_options = True

    def __init__(self, session: _MockSandboxSession | None = None) -> None:
        self.inner_session = session or _MockSandboxSession()
        self.session = self._wrap_session(self.inner_session)
        self.create_calls: int = 0
        self.resume_calls: int = 0
        self.delete_calls: int = 0
        self.create_options: list[BaseSandboxClientOptions | None] = []

    async def create(
        self,
        *,
        snapshot: Any = None,
        manifest: Manifest | None = None,
        options: BaseSandboxClientOptions | None = None,
    ) -> SandboxSession:
        self.create_calls += 1
        self.create_options.append(options)
        if manifest is not None:
            self.inner_session.state.manifest = manifest
        return self.session

    async def resume(self, state: SandboxSessionState) -> SandboxSession:
        self.resume_calls += 1
        self.inner_session.state = state
        return self.session

    async def delete(self, session: SandboxSession) -> SandboxSession:
        self.delete_calls += 1
        return session

    def deserialize_session_state(self, payload: dict[str, Any]) -> SandboxSessionState:
        return SandboxSessionState.model_validate(payload)


# ── SandboxClientProvider unit tests (delegation) ──


@pytest.fixture
def mock_client() -> _MockSandboxClient:
    return _MockSandboxClient()


@pytest.fixture
def sandbox_activities(mock_client: _MockSandboxClient) -> SandboxClientProvider:
    return SandboxClientProvider("mock", mock_client)


def _make_state(manifest: Manifest | None = None) -> TestSessionState:
    return TestSessionState(
        manifest=manifest or Manifest(),
        snapshot=NoopSnapshot(id=str(uuid.uuid4())),
    )


def _activity_map(
    sandbox_activities: SandboxClientProvider,
) -> dict[str, Any]:
    """Build a short-name → callable dict from all() for easy test dispatch."""
    return {
        act.__temporal_activity_definition.name: act  # type: ignore[attr-defined, union-attr]
        for act in sandbox_activities._get_activities()
    }


async def test_activities_create_session_delegates(
    sandbox_activities: SandboxClientProvider,
    mock_client: _MockSandboxClient,
):
    """create_session activity should delegate to the real client's create()."""
    acts = _activity_map(sandbox_activities)
    args = CreateSessionArgs(
        snapshot_spec=None,
        manifest=Manifest(),
        client_options=None,
    )
    result = await acts["mock-sandbox_client_create"](args)
    assert mock_client.create_calls == 1
    assert result.state is not None
    assert isinstance(result.supports_pty, bool)


async def test_activities_resume_session_delegates(
    sandbox_activities: SandboxClientProvider,
    mock_client: _MockSandboxClient,
):
    """resume_session activity should delegate to the real client's resume()."""
    acts = _activity_map(sandbox_activities)
    state = _make_state()
    args = ResumeSessionArgs(state=state)
    result = await acts["mock-sandbox_client_resume"](args)
    assert mock_client.resume_calls == 1
    assert result.state is not None


async def test_activities_exec_delegates(
    sandbox_activities: SandboxClientProvider,
    mock_client: _MockSandboxClient,
):
    """exec activity should delegate to the real session's exec()."""
    acts = _activity_map(sandbox_activities)
    # First create a session so the activities cache is populated
    await acts["mock-sandbox_client_create"](
        CreateSessionArgs(snapshot_spec=None, manifest=Manifest(), client_options=None)
    )
    state = mock_client.inner_session.state

    args = ExecArgs(state=state, command=["echo", "hello"], timeout=10.0, shell=True)
    result = await acts["mock-sandbox_session_exec"](args)
    assert result.stdout == b"ok\n"
    assert result.stderr == b""
    assert result.exit_code == 0
    assert len(mock_client.inner_session.exec_calls) == 1


async def test_activities_read_delegates(
    sandbox_activities: SandboxClientProvider,
    mock_client: _MockSandboxClient,
):
    """read activity should delegate to the real session's read()."""
    acts = _activity_map(sandbox_activities)
    await acts["mock-sandbox_client_create"](
        CreateSessionArgs(snapshot_spec=None, manifest=Manifest(), client_options=None)
    )
    state = mock_client.inner_session.state

    args = ReadArgs(state=state, path="/tmp/test.txt")
    result = await acts["mock-sandbox_session_read"](args)
    assert result.data == b"file-content"
    assert len(mock_client.inner_session.read_calls) == 1


async def test_activities_write_delegates(
    sandbox_activities: SandboxClientProvider,
    mock_client: _MockSandboxClient,
):
    """write activity should delegate to the real session's write()."""
    acts = _activity_map(sandbox_activities)
    await acts["mock-sandbox_client_create"](
        CreateSessionArgs(snapshot_spec=None, manifest=Manifest(), client_options=None)
    )
    state = mock_client.inner_session.state

    args = WriteArgs(state=state, path="/tmp/out.txt", data=b"written-data")
    await acts["mock-sandbox_session_write"](args)
    assert len(mock_client.inner_session.write_calls) == 1
    assert mock_client.inner_session.write_calls[0][1] == b"written-data"


async def test_activities_running_delegates(
    sandbox_activities: SandboxClientProvider,
    mock_client: _MockSandboxClient,
):
    """running activity should delegate to the real session's running()."""
    acts = _activity_map(sandbox_activities)
    await acts["mock-sandbox_client_create"](
        CreateSessionArgs(snapshot_spec=None, manifest=Manifest(), client_options=None)
    )
    state = mock_client.inner_session.state

    args = RunningArgs(state=state)
    result = await acts["mock-sandbox_session_running"](args)
    assert result.is_running is True
    assert mock_client.inner_session.running_calls == 1


async def test_activities_client_delete_delegates(
    sandbox_activities: SandboxClientProvider,
    mock_client: _MockSandboxClient,
):
    """client_delete activity should delegate to the real client's delete()."""
    acts = _activity_map(sandbox_activities)
    await acts["mock-sandbox_client_create"](
        CreateSessionArgs(snapshot_spec=None, manifest=Manifest(), client_options=None)
    )
    state = mock_client.inner_session.state

    args = StopArgs(state=state)
    await acts["mock-sandbox_client_delete"](args)

    assert mock_client.delete_calls == 1


async def test_activities_session_shutdown_clears_cache(
    sandbox_activities: SandboxClientProvider,
    mock_client: _MockSandboxClient,
):
    """session_shutdown activity should call session.shutdown() and evict from cache."""
    acts = _activity_map(sandbox_activities)
    await acts["mock-sandbox_client_create"](
        CreateSessionArgs(snapshot_spec=None, manifest=Manifest(), client_options=None)
    )
    state = mock_client.inner_session.state
    session_key = str(state.session_id)

    # Session should be cached
    assert session_key in sandbox_activities._sessions

    args = StopArgs(state=state)
    await acts["mock-sandbox_session_shutdown"](args)

    assert mock_client.inner_session.shutdown_calls == 1
    # Session should be evicted from cache
    assert session_key not in sandbox_activities._sessions


async def test_activities_session_shutdown_noop_for_unknown_session(
    sandbox_activities: SandboxClientProvider,
):
    """session_shutdown should be a no-op if the session isn't in the cache."""
    acts = _activity_map(sandbox_activities)
    state = _make_state()
    args = StopArgs(state=state)
    # Should not raise
    await acts["mock-sandbox_session_shutdown"](args)


async def test_activities_session_caching(
    sandbox_activities: SandboxClientProvider,
    mock_client: _MockSandboxClient,
):
    """Multiple operations on the same session should reuse the cached session."""
    acts = _activity_map(sandbox_activities)
    await acts["mock-sandbox_client_create"](
        CreateSessionArgs(snapshot_spec=None, manifest=Manifest(), client_options=None)
    )
    state = mock_client.inner_session.state

    # Multiple exec calls should not trigger additional resume calls
    await acts["mock-sandbox_session_exec"](
        ExecArgs(state=state, command=["cmd1"], shell=True)
    )
    await acts["mock-sandbox_session_exec"](
        ExecArgs(state=state, command=["cmd2"], shell=True)
    )
    assert mock_client.resume_calls == 0
    assert len(mock_client.inner_session.exec_calls) == 2


async def test_activities_all_returns_all_activity_methods(
    sandbox_activities: SandboxClientProvider,
):
    """all() should return all 14 activity callables with prefixed names."""
    activities = sandbox_activities._get_activities()
    assert len(activities) == 14
    # Verify they are all activity-decorated callables with prefixed names
    activity_names = set()
    for act in activities:
        assert hasattr(act, "__temporal_activity_definition")
        activity_names.add(act.__temporal_activity_definition.name)  # type: ignore[union-attr]
    expected = {
        "mock-sandbox_client_create",
        "mock-sandbox_client_resume",
        "mock-sandbox_client_delete",
        "mock-sandbox_session_exec",
        "mock-sandbox_session_read",
        "mock-sandbox_session_write",
        "mock-sandbox_session_running",
        "mock-sandbox_session_persist_workspace",
        "mock-sandbox_session_hydrate_workspace",
        "mock-sandbox_session_pty_exec_start",
        "mock-sandbox_session_pty_write_stdin",
        "mock-sandbox_session_start",
        "mock-sandbox_session_stop",
        "mock-sandbox_session_shutdown",
    }
    assert activity_names == expected


async def test_multiple_providers_register_distinct_activities():
    """Multiple SandboxClientProviders should produce distinct prefixed activity sets."""
    client1 = _MockSandboxClient()
    client2 = _MockSandboxClient()
    provider1 = SandboxClientProvider("daytona", client1)
    provider2 = SandboxClientProvider("local", client2)

    activities1 = provider1._get_activities()
    activities2 = provider2._get_activities()

    names1 = {a.__temporal_activity_definition.name for a in activities1}  # type: ignore
    names2 = {a.__temporal_activity_definition.name for a in activities2}  # type: ignore

    # No overlap
    assert names1.isdisjoint(names2)
    # Both have 14 activities
    assert len(names1) == 14
    assert len(names2) == 14
    # Verify prefixes
    assert all(
        n.startswith("daytona-sandbox_client_")
        or n.startswith("daytona-sandbox_session_")
        for n in names1
    )
    assert all(
        n.startswith("local-sandbox_client_") or n.startswith("local-sandbox_session_")
        for n in names2
    )


# ── SandboxError retryable mapping tests ──


class _ExecRaisingSession(_MockSandboxSession):
    """Mock session whose exec() raises a chosen SandboxError."""

    def __init__(self, error: SandboxError) -> None:
        super().__init__()
        self._error = error

    async def _exec_internal(
        self,
        *command: str | Path,  # type: ignore[reportUnusedParameter]
        timeout: float | None = None,  # type: ignore[reportUnusedParameter]
    ) -> ExecResult:
        raise self._error


async def _exec_with_error(error: SandboxError) -> None:
    provider = SandboxClientProvider(
        "mock", _MockSandboxClient(_ExecRaisingSession(error))
    )
    acts = _activity_map(provider)
    state = (
        await acts["mock-sandbox_client_create"](
            CreateSessionArgs(
                snapshot_spec=None, manifest=Manifest(), client_options=None
            )
        )
    ).state
    await acts["mock-sandbox_session_exec"](
        ExecArgs(state=state, command=["boom"], shell=True)
    )


async def test_exec_terminal_error_becomes_non_retryable_application_error():
    """retryable is False should map to a non-retryable ApplicationError."""
    with pytest.raises(ApplicationError) as exc_info:
        await _exec_with_error(ExecTransportError(command=["boom"], retryable=False))
    assert exc_info.value.non_retryable is True
    assert exc_info.value.type == "exec_transport_error"


async def test_exec_transient_error_propagates_unchanged():
    """retryable is True should let the original SandboxError propagate."""
    with pytest.raises(ExecTransportError):
        await _exec_with_error(ExecTransportError(command=["boom"], retryable=True))


async def test_exec_unclassified_error_propagates_unchanged():
    """retryable is None should let the original SandboxError propagate (not converted)."""
    with pytest.raises(ExecTransportError):
        await _exec_with_error(ExecTransportError(command=["boom"], retryable=None))


class _ShutdownRaisingSession(_MockSandboxSession):
    """Mock session whose shutdown() raises a chosen SandboxError."""

    def __init__(self, error: SandboxError) -> None:
        super().__init__()
        self._error = error

    async def shutdown(self) -> None:
        raise self._error


async def _create_shutdown_raising(
    error: SandboxError,
) -> tuple[dict[str, Any], SandboxClientProvider, StopArgs, str]:
    provider = SandboxClientProvider(
        "mock", _MockSandboxClient(_ShutdownRaisingSession(error))
    )
    acts = _activity_map(provider)
    state = (
        await acts["mock-sandbox_client_create"](
            CreateSessionArgs(
                snapshot_spec=None, manifest=Manifest(), client_options=None
            )
        )
    ).state
    key = str(state.session_id)
    assert key in provider._sessions
    return acts, provider, StopArgs(state=state), key


async def test_shutdown_terminal_error_evicts_session_and_raises():
    """A terminal shutdown error maps to a non-retryable ApplicationError and
    evicts the dead session from the cache."""
    acts, provider, args, key = await _create_shutdown_raising(
        ExecTransportError(command=["shutdown"], retryable=False)
    )

    with pytest.raises(ApplicationError) as exc_info:
        await acts["mock-sandbox_session_shutdown"](args)
    assert exc_info.value.non_retryable is True
    assert key not in provider._sessions


async def test_shutdown_retryable_error_keeps_session_cached():
    """A retryable shutdown error propagates unchanged and leaves the session
    cached so the activity's retry can still shut it down."""
    acts, provider, args, key = await _create_shutdown_raising(
        ExecTransportError(command=["shutdown"], retryable=True)
    )

    with pytest.raises(ExecTransportError):
        await acts["mock-sandbox_session_shutdown"](args)
    assert key in provider._sessions


class _RunningRaisingSession(_MockSandboxSession):
    """Mock session whose running() raises a chosen SandboxError."""

    def __init__(self, error: SandboxError) -> None:
        super().__init__()
        self._error = error

    async def running(self) -> bool:
        raise self._error


async def test_running_terminal_error_becomes_non_retryable_application_error():
    """A terminal SandboxError from a non-exec activity also maps to a
    non-retryable ApplicationError, with type set to its error_code."""
    error = WorkspaceArchiveReadError(path=Path("/workspace"), retryable=False)
    provider = SandboxClientProvider(
        "mock", _MockSandboxClient(_RunningRaisingSession(error))
    )
    acts = _activity_map(provider)
    state = (
        await acts["mock-sandbox_client_create"](
            CreateSessionArgs(
                snapshot_spec=None, manifest=Manifest(), client_options=None
            )
        )
    ).state

    with pytest.raises(ApplicationError) as exc_info:
        await acts["mock-sandbox_session_running"](RunningArgs(state=state))
    assert exc_info.value.non_retryable is True
    assert exc_info.value.type == "workspace_archive_read_error"


# ── End-to-end test: Runner + SandboxAgent through Temporal activities ──


class _TestSandboxCapability(Capability):
    """Minimal capability exposing exec, read, and write via FunctionTools."""

    def __init__(self) -> None:
        super().__init__(type="test_sandbox")
        self._session: BaseSandboxSession | None = None

    def bind(self, session: BaseSandboxSession) -> None:
        self._session = session

    def tools(self) -> list[Tool]:
        session = self._session

        async def _run_cmd(ctx: Any, args: str) -> str:  # type: ignore[reportUnusedParameter]
            import json

            cmd = json.loads(args)["cmd"]
            result = await session.exec(cmd, shell=True)  # type: ignore[union-attr]
            return result.stdout.decode()

        async def _read_file(ctx: Any, args: str) -> str:  # type: ignore[reportUnusedParameter]
            import json

            path = json.loads(args)["path"]
            handle = await session.read(Path(path))  # type: ignore[union-attr]
            return handle.read().decode()

        async def _write_file(ctx: Any, args: str) -> str:  # type: ignore[reportUnusedParameter]
            import json

            parsed = json.loads(args)
            await session.write(  # type: ignore[union-attr]
                Path(parsed["path"]), io.BytesIO(parsed["data"].encode())
            )
            return "ok"

        return [
            FunctionTool(
                name="run_command",
                description="Run a shell command",
                params_json_schema={
                    "type": "object",
                    "properties": {"cmd": {"type": "string"}},
                    "required": ["cmd"],
                },
                on_invoke_tool=_run_cmd,
            ),
            FunctionTool(
                name="read_file",
                description="Read a file",
                params_json_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                on_invoke_tool=_read_file,
            ),
            FunctionTool(
                name="write_file",
                description="Write a file",
                params_json_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "data": {"type": "string"},
                    },
                    "required": ["path", "data"],
                },
                on_invoke_tool=_write_file,
            ),
        ]


class _TestSandboxClientOptions(BaseSandboxClientOptions):
    type: str = "test"  # type: ignore[reportIncompatibleVariableOverride]


@workflow.defn
class SandboxE2EWorkflow:
    @workflow.run
    async def run(self) -> str:
        agent = SandboxAgent[None](
            name="sandbox-e2e", capabilities=[_TestSandboxCapability()]
        )
        result = await Runner.run(
            starting_agent=agent,
            input="run a command",
            run_config=RunConfig(
                sandbox=SandboxRunConfig(
                    client=temporal_sandbox_client("mock"),
                    options=_TestSandboxClientOptions(),
                ),
            ),
        )
        return result.final_output


def _client_with_plugin(client: Client, plugin: OpenAIAgentsPlugin) -> Client:
    new_config = client.config()
    new_config["plugins"] = [plugin]
    return Client(**new_config)


async def test_sandbox_e2e_runner(client: Client):
    """End-to-end: Runner.run() with SandboxAgent exercises the full sandbox
    lifecycle (create, start, stop, shutdown, delete) through Temporal activities."""
    mock_session = _MockSandboxSession()
    mock_sandbox_client = _MockSandboxClient(mock_session)

    mock_model = TestModel.returning_responses(
        [
            ResponseBuilders.tool_call('{"cmd": "echo hello"}', "run_command"),
            ResponseBuilders.tool_call('{"path": "/tmp/test.txt"}', "read_file"),
            ResponseBuilders.tool_call(
                '{"path": "/tmp/out.txt", "data": "hello"}', "write_file"
            ),
            ResponseBuilders.output_message("Done."),
        ]
    )

    plugin = OpenAIAgentsPlugin(
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
        ),
        model_provider=TestModelProvider(mock_model),
        sandbox_clients=[SandboxClientProvider("mock", mock_sandbox_client)],
    )

    test_client = _client_with_plugin(client, plugin)

    async with new_worker(
        test_client,
        SandboxE2EWorkflow,
        workflow_failure_exception_types=[Exception],
    ) as worker:
        result = await test_client.execute_workflow(
            SandboxE2EWorkflow.run,
            id=f"sandbox-e2e-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=10),
        )

    assert result == "Done."
    # Full sandbox lifecycle exercised through Temporal activities
    assert mock_sandbox_client.create_calls == 1, "client.create() not called"
    assert mock_session.start_calls == 1, "session.start() not called"
    assert len(mock_session.exec_calls) >= 1, "session.exec() not called"
    assert len(mock_session.read_calls) >= 1, "session.read() not called"
    assert len(mock_session.write_calls) >= 1, "session.write() not called"
    assert mock_session.stop_calls >= 1, "session.stop() not called"
    assert mock_session.shutdown_calls >= 1, "session.shutdown() not called"
    assert mock_sandbox_client.delete_calls == 1, "client.delete() not called"


# ── Default (omitted) client options ──

_SANDBOX_OPTIONS_SECRET = "sk-sandbox-must-not-reach-history"


class _SecretSandboxClientOptions(BaseSandboxClientOptions):
    type: str = "secret-test"  # type: ignore[reportIncompatibleVariableOverride]
    # Required so the workflow must set it: ``exclude_unset`` drops a defaulted field.
    api_key: str


def test_temporal_sandbox_client_supports_default_options_false_by_default():
    assert temporal_sandbox_client("my-backend").supports_default_options is False


def test_temporal_sandbox_client_supports_default_options():
    client = temporal_sandbox_client("my-backend", supports_default_options=True)
    assert client.supports_default_options is True


@workflow.defn
class SandboxOptionsRequiredWorkflow:
    @workflow.run
    async def run(self) -> str:
        try:
            await Runner.run(
                starting_agent=SandboxAgent[None](name="sandbox-no-options"),
                input="hello",
                run_config=RunConfig(
                    sandbox=SandboxRunConfig(client=temporal_sandbox_client("mock")),
                ),
            )
        except ValueError as e:
            return str(e)
        return "FAIL: omitting options should have been rejected"


async def test_sandbox_options_required_by_default(client: Client):
    async with AgentEnvironment(model=_mock_model()) as env:
        client = env.applied_on_client(client)
        async with new_worker(
            client,
            SandboxOptionsRequiredWorkflow,
            workflow_failure_exception_types=[ValueError, AssertionError],
        ) as worker:
            result = await client.execute_workflow(
                SandboxOptionsRequiredWorkflow.run,
                id=f"sandbox-options-required-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=10),
            )
    assert "requires `run_config.sandbox.options`" in result


@workflow.defn
class SandboxDefaultOptionsWorkflow:
    @workflow.run
    async def run(self, include_options: bool) -> str:
        agent = SandboxAgent[None](
            name="sandbox-default-options", capabilities=[_TestSandboxCapability()]
        )
        result = await Runner.run(
            starting_agent=agent,
            input="run a command",
            run_config=RunConfig(
                sandbox=SandboxRunConfig(
                    client=temporal_sandbox_client(
                        "mock", supports_default_options=True
                    ),
                    options=_SecretSandboxClientOptions(api_key=_SANDBOX_OPTIONS_SECRET)
                    if include_options
                    else None,
                ),
            ),
        )
        return result.final_output


def _mock_sandbox_plugin(provider: SandboxClientProvider) -> OpenAIAgentsPlugin:
    return OpenAIAgentsPlugin(
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
        ),
        model_provider=TestModelProvider(
            TestModel.returning_responses(
                [
                    ResponseBuilders.tool_call('{"cmd": "echo one"}', "run_command"),
                    ResponseBuilders.tool_call('{"cmd": "echo two"}', "run_command"),
                    ResponseBuilders.output_message("Done."),
                ]
            )
        ),
        sandbox_clients=[provider],
    )


def _activity_payloads(
    events: Sequence[HistoryEvent], activity_name: str
) -> tuple[list[bytes], list[bytes]]:
    """The ``(arguments, results)`` payload bytes of every ``activity_name`` activity."""
    scheduled_ids: set[int] = set()
    args: list[bytes] = []
    results: list[bytes] = []
    for event in events:
        if event.HasField("activity_task_scheduled_event_attributes"):
            scheduled = event.activity_task_scheduled_event_attributes
            if scheduled.activity_type.name == activity_name:
                scheduled_ids.add(event.event_id)
                args.extend(p.data for p in scheduled.input.payloads)
        elif event.HasField("activity_task_completed_event_attributes"):
            completed = event.activity_task_completed_event_attributes
            # A completion event carries no activity type, so the only way to
            # attribute a result is through the scheduled event it points back to.
            if completed.scheduled_event_id in scheduled_ids:
                results.extend(p.data for p in completed.result.payloads)
    return args, results


@pytest.mark.parametrize("include_options", [False, True], ids=["omitted", "provided"])
async def test_sandbox_client_options_reach_history_only_when_provided(
    client: Client, include_options: bool
):
    mock_sandbox_client = _MockSandboxClient()
    provider = SandboxClientProvider("mock", mock_sandbox_client)
    test_client = _client_with_plugin(client, _mock_sandbox_plugin(provider))

    async with new_worker(
        test_client,
        SandboxDefaultOptionsWorkflow,
        workflow_failure_exception_types=[Exception],
    ) as worker:
        handle = await test_client.start_workflow(
            SandboxDefaultOptionsWorkflow.run,
            include_options,
            id=f"sandbox-default-options-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=20),
        )
        assert await handle.result() == "Done."
        events = (await handle.fetch_history()).events

    payloads, _ = _activity_payloads(events, "mock-sandbox_client_create")
    assert len(payloads) == 1
    serialized = json.loads(payloads[0])
    if include_options:
        assert _SANDBOX_OPTIONS_SECRET.encode() in payloads[0]
        assert serialized["client_options"]["api_key"] == _SANDBOX_OPTIONS_SECRET
        assert isinstance(
            mock_sandbox_client.create_options[0], _SecretSandboxClientOptions
        )
    else:
        assert _SANDBOX_OPTIONS_SECRET.encode() not in payloads[0]
        assert serialized["client_options"] is None
        assert mock_sandbox_client.create_options == [None]


class _CacheEvictingSession(_MockSandboxSession):
    """Mock session that drops the provider's session cache after its first exec,
    so the next operation has to take the implicit-resume path."""

    def __init__(self) -> None:
        super().__init__()
        self.evict: Callable[[], None] | None = None

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        if self.evict is not None:
            self.evict()
            self.evict = None
        return await super()._exec_internal(*command, timeout=timeout)


async def test_sandbox_default_options_survive_worker_cache_miss(client: Client):
    """Options only reach the create activity, so an implicit resume must not need them."""
    inner_session = _CacheEvictingSession()
    mock_sandbox_client = _MockSandboxClient(inner_session)
    provider = SandboxClientProvider("mock", mock_sandbox_client)
    inner_session.evict = provider._sessions.clear
    test_client = _client_with_plugin(client, _mock_sandbox_plugin(provider))

    async with new_worker(
        test_client,
        SandboxDefaultOptionsWorkflow,
        workflow_failure_exception_types=[Exception],
    ) as worker:
        result = await test_client.execute_workflow(
            SandboxDefaultOptionsWorkflow.run,
            False,
            id=f"sandbox-cache-miss-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=20),
        )

    assert result == "Done."
    assert mock_sandbox_client.create_options == [None]
    assert len(inner_session.exec_calls) == 2
    # One resume for the second exec, one for teardown after shutdown evicts again.
    assert mock_sandbox_client.resume_calls == 2


class _OptionsRequiringSandboxClient(_MockSandboxClient):
    """Mock of a backend that reads a required field off ``options``."""

    # Overrides the base mock's ``True``, which is what makes the guard fire.
    supports_default_options = False

    def __init__(self) -> None:
        super().__init__()
        self.create_entries: int = 0

    async def create(
        self,
        *,
        snapshot: Any = None,
        manifest: Manifest | None = None,
        options: BaseSandboxClientOptions | None = None,
    ) -> SandboxSession:
        self.create_entries += 1
        _ = options.image  # type: ignore[attr-defined, union-attr]
        return await super().create(
            snapshot=snapshot, manifest=manifest, options=options
        )


@workflow.defn
class SandboxDefaultOptionsUnsupportedWorkflow:
    @workflow.run
    async def run(self) -> str:
        result = await Runner.run(
            starting_agent=SandboxAgent[None](name="sandbox-needs-options"),
            input="hello",
            run_config=RunConfig(
                sandbox=SandboxRunConfig(
                    client=temporal_sandbox_client(
                        "needs-options", supports_default_options=True
                    ),
                ),
            ),
        )
        return result.final_output


async def test_sandbox_default_options_rejected_when_client_requires_them(
    client: Client,
):
    """Claiming default options for a client that requires them fails the workflow fast."""
    mock_sandbox_client = _OptionsRequiringSandboxClient()
    provider = SandboxClientProvider("needs-options", mock_sandbox_client)
    test_client = _client_with_plugin(client, _mock_sandbox_plugin(provider))

    async with new_worker(
        test_client,
        SandboxDefaultOptionsUnsupportedWorkflow,
        workflow_failure_exception_types=[Exception],
    ) as worker:
        with pytest.raises(WorkflowFailureError) as exc_info:
            await test_client.execute_workflow(
                SandboxDefaultOptionsUnsupportedWorkflow.run,
                id=f"sandbox-needs-options-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=15),
            )

    causes: list[BaseException] = []
    err: BaseException | None = exc_info.value
    while err is not None:
        causes.append(err)
        err = err.__cause__
    app_error = next(e for e in causes if isinstance(e, ApplicationError))
    assert app_error.non_retryable is True
    assert app_error.type == "sandbox_options_required"
    assert "supports_default_options" in str(app_error)
    assert "'needs-options'" in str(app_error)
    assert mock_sandbox_client.create_entries == 0


@workflow.defn
class SandboxUnixLocalWorkflow:
    @workflow.run
    async def run(self) -> str:
        agent = SandboxAgent[None](
            name="sandbox-unix-local", capabilities=[_TestSandboxCapability()]
        )
        result = await Runner.run(
            starting_agent=agent,
            input="write then read a file",
            run_config=RunConfig(
                sandbox=SandboxRunConfig(
                    client=temporal_sandbox_client(
                        "unix-local", supports_default_options=True
                    ),
                ),
            ),
        )
        return result.final_output


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="agents.sandbox.sandboxes.unix_local raises ImportError at import time on Windows.",
)
async def test_sandbox_default_options_unix_local(client: Client):
    """A real backend that advertises default options creates a working session
    from an activity argument that carries no options at all."""
    from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

    plugin = OpenAIAgentsPlugin(
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
        ),
        model_provider=TestModelProvider(
            TestModel.returning_responses(
                [
                    ResponseBuilders.tool_call(
                        '{"path": "greeting.txt", "data": "from-unix-local"}',
                        "write_file",
                    ),
                    ResponseBuilders.tool_call(
                        '{"cmd": "cat greeting.txt"}', "run_command"
                    ),
                    ResponseBuilders.output_message("Done."),
                ]
            )
        ),
        sandbox_clients=[SandboxClientProvider("unix-local", UnixLocalSandboxClient())],
    )
    test_client = _client_with_plugin(client, plugin)

    async with new_worker(
        test_client,
        SandboxUnixLocalWorkflow,
        workflow_failure_exception_types=[Exception],
    ) as worker:
        handle = await test_client.start_workflow(
            SandboxUnixLocalWorkflow.run,
            id=f"sandbox-unix-local-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )
        assert await handle.result() == "Done."
        events = (await handle.fetch_history()).events

    create_args, _ = _activity_payloads(events, "unix-local-sandbox_client_create")
    assert len(create_args) == 1
    assert json.loads(create_args[0])["client_options"] is None

    _, exec_results = _activity_payloads(events, "unix-local-sandbox_session_exec")
    stdouts = [json.loads(result)["stdout"] for result in exec_results]
    # ``ExecResult.stdout`` is ``JsonSafeBytes``, so history holds it base64-encoded.
    assert b64encode(b"from-unix-local").decode() in stdouts


# ── JsonSafeBytes lossless serialization tests ──

# Payloads that exercise edge cases for bytes → JSON → bytes roundtrip.
_BYTE_PAYLOADS = [
    pytest.param(b"", id="empty"),
    pytest.param(b"hello world", id="ascii"),
    pytest.param(b"\xc3\xa9\xc3\xa0", id="valid-utf8"),  # éà
    pytest.param(bytes(range(256)), id="all-byte-values"),
    pytest.param(b"\xff\xfe\x80\x90\x00\x01", id="non-utf8-binary"),
    pytest.param(b"ok\nWarning: \xff\xfe binary \x80\x90\x00\x01", id="mixed"),
    pytest.param(b"\x00\x00\x00", id="null-bytes"),
]


def _roundtrip(model_cls: Any, **kwargs: Any) -> Any:
    """Serialize a model to JSON via pydantic_core and deserialize back."""
    json_bytes = to_json(model_cls(**kwargs))
    return TypeAdapter(model_cls).validate_json(json_bytes)


@pytest.mark.parametrize("payload", _BYTE_PAYLOADS)
def test_exec_result_bytes_roundtrip(payload: bytes):
    """ExecResult.stdout/stderr must survive a JSON roundtrip unchanged."""
    restored = _roundtrip(ExecResultModel, stdout=payload, stderr=payload, exit_code=1)
    assert restored.stdout == payload
    assert restored.stderr == payload
    assert restored.exit_code == 1


@pytest.mark.parametrize("payload", _BYTE_PAYLOADS)
def test_pty_exec_update_result_bytes_roundtrip(payload: bytes):
    """PtyExecUpdateResult.output must survive a JSON roundtrip unchanged."""
    restored = _roundtrip(
        PtyExecUpdateResult,
        process_id=1,
        output=payload,
        exit_code=0,
        original_token_count=None,
    )
    assert restored.output == payload


@pytest.mark.parametrize("payload", _BYTE_PAYLOADS)
def test_read_result_bytes_roundtrip(payload: bytes):
    """ReadResult.data must survive a JSON roundtrip unchanged."""
    restored = _roundtrip(ReadResult, data=payload)
    assert restored.data == payload


@pytest.mark.parametrize("payload", _BYTE_PAYLOADS)
def test_persist_workspace_result_bytes_roundtrip(payload: bytes):
    """PersistWorkspaceResult.data must survive a JSON roundtrip unchanged."""
    restored = _roundtrip(PersistWorkspaceResult, data=payload)
    assert restored.data == payload


@pytest.mark.parametrize("payload", _BYTE_PAYLOADS)
def test_write_args_bytes_roundtrip(payload: bytes):
    """WriteArgs.data must survive a JSON roundtrip unchanged (workflow → activity)."""
    restored = _roundtrip(WriteArgs, state=_make_state(), path="/tmp/f", data=payload)
    assert restored.data == payload


@pytest.mark.parametrize("payload", _BYTE_PAYLOADS)
def test_hydrate_workspace_args_bytes_roundtrip(payload: bytes):
    """HydrateWorkspaceArgs.data must survive a JSON roundtrip unchanged."""
    restored = _roundtrip(HydrateWorkspaceArgs, state=_make_state(), data=payload)
    assert restored.data == payload
