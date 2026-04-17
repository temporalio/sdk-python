"""Tests for sandbox validation in TemporalOpenAIRunner."""

import io
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
from agents import Agent, FunctionTool, RunConfig, Runner, Tool
from agents.sandbox import Capability, Manifest, SandboxAgent, SandboxRunConfig
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
from temporalio.client import Client
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

    async def create(
        self,
        *,
        snapshot: Any = None,
        manifest: Manifest | None = None,
        options: BaseSandboxClientOptions | None = None,
    ) -> SandboxSession:
        self.create_calls += 1
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

    new_config = client.config()
    new_config["plugins"] = [plugin]
    test_client = Client(**new_config)

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
