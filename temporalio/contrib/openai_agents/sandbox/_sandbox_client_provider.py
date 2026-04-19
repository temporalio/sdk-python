"""Public-facing provider that pairs a name with a real sandbox client."""

from __future__ import annotations

import io
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from agents.sandbox.session.sandbox_client import BaseSandboxClient
from agents.sandbox.session.sandbox_session import SandboxSession

from temporalio import activity
from temporalio.contrib.openai_agents.sandbox._temporal_activity_models import (
    CreateSessionArgs,
    ExecArgs,
    HydrateWorkspaceArgs,
    PersistWorkspaceArgs,
    PersistWorkspaceResult,
    PtyExecStartArgs,
    PtyExecUpdateResult,
    PtyWriteStdinArgs,
    ReadArgs,
    ReadResult,
    ResumeSessionArgs,
    RunningArgs,
    RunningResult,
    SessionResult,
    StartArgs,
    StopArgs,
    WriteArgs,
    _HasState,
)
from temporalio.contrib.openai_agents.sandbox._temporal_activity_models import (
    ExecResult as ExecResultModel,
)


class SandboxClientProvider:
    """A named sandbox client provider for Temporal workflows.

    .. warning::
        This class is experimental and may change in future versions.
        Use with caution in production environments.

    Wraps a ``BaseSandboxClient`` with a unique name so that multiple
    sandbox backends can be registered on a single Temporal worker.  Each
    provider gets its own set of Temporal activities whose names are prefixed
    with the provider name, allowing them to coexist on the same task queue.

    On the **worker side**, pass one or more providers to the plugin::

        plugin = OpenAIAgentsPlugin(
            sandbox_clients=[
                SandboxClientProvider("daytona", DaytonaSandboxClient()),
                SandboxClientProvider("local", UnixLocalSandboxClient()),
            ],
        )

    On the **workflow side**, reference a provider by name via
    :func:`temporalio.contrib.openai_agents.workflow.temporal_sandbox_client`::

        run_config = RunConfig(
            sandbox=SandboxRunConfig(
                client=temporal_sandbox_client("daytona"),
                ...
            ),
        )

    Args:
        name: A unique name for this sandbox backend (e.g. ``"daytona"``,
            ``"local"``).  Must match the name used on the workflow side.
        client: The real ``BaseSandboxClient`` that performs sandbox
            lifecycle and I/O operations on the worker.
    """

    def __init__(self, name: str, client: BaseSandboxClient[Any]) -> None:
        """Initialize the provider."""
        self._name = name
        self._client = client
        self._sessions: dict[str, SandboxSession] = {}

    @property
    def name(self) -> str:
        """The provider name used as an activity-name prefix."""
        return self._name

    async def _session(self, args: _HasState) -> SandboxSession:
        key = str(args.state.session_id)
        if key not in self._sessions:
            self._sessions[key] = await self._client.resume(args.state)
        return self._sessions[key]

    def _get_activities(self) -> Sequence[Callable[..., Any]]:
        """Return all activity callables for registration with a Temporal Worker."""
        prefix = self._name

        # -- Client-level operations (lifecycle) --

        @activity.defn(name=f"{prefix}-sandbox_client_create")
        async def create_session(args: CreateSessionArgs) -> SessionResult:
            session = await self._client.create(
                snapshot=args.snapshot_spec,
                manifest=args.manifest,
                options=args.client_options,
            )
            self._sessions[str(session.state.session_id)] = session
            return SessionResult(
                state=session.state, supports_pty=session.supports_pty()
            )

        @activity.defn(name=f"{prefix}-sandbox_client_resume")
        async def resume_session(args: ResumeSessionArgs) -> SessionResult:
            session = await self._client.resume(args.state)
            self._sessions[str(session.state.session_id)] = session
            return SessionResult(
                state=session.state, supports_pty=session.supports_pty()
            )

        @activity.defn(name=f"{prefix}-sandbox_client_delete")
        async def delete_session(args: StopArgs) -> None:
            session = await self._session(args)
            await self._client.delete(session)
            return None

        # -- Session-level operations (I/O and lifecycle) --

        @activity.defn(name=f"{prefix}-sandbox_session_exec")
        async def exec_(args: ExecArgs) -> ExecResultModel:
            session = await self._session(args)
            result = await session.exec(
                *args.command,
                timeout=args.timeout,
                shell=args.shell,
                user=args.user,
            )
            return ExecResultModel(
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
            )

        @activity.defn(name=f"{prefix}-sandbox_session_read")
        async def read(args: ReadArgs) -> ReadResult:
            session = await self._session(args)
            handle = await session.read(Path(args.path))
            return ReadResult(data=handle.read())

        @activity.defn(name=f"{prefix}-sandbox_session_write")
        async def write(args: WriteArgs) -> None:
            session = await self._session(args)
            await session.write(Path(args.path), io.BytesIO(args.data))
            return None

        @activity.defn(name=f"{prefix}-sandbox_session_running")
        async def running(args: RunningArgs) -> RunningResult:
            session = await self._session(args)
            return RunningResult(is_running=await session.running())

        @activity.defn(name=f"{prefix}-sandbox_session_persist_workspace")
        async def persist_workspace(
            args: PersistWorkspaceArgs,
        ) -> PersistWorkspaceResult:
            session = await self._session(args)
            stream = await session.persist_workspace()
            return PersistWorkspaceResult(data=stream.read())

        @activity.defn(name=f"{prefix}-sandbox_session_hydrate_workspace")
        async def hydrate_workspace(args: HydrateWorkspaceArgs) -> None:
            session = await self._session(args)
            await session.hydrate_workspace(io.BytesIO(args.data))
            return None

        @activity.defn(name=f"{prefix}-sandbox_session_pty_exec_start")
        async def pty_exec_start(args: PtyExecStartArgs) -> PtyExecUpdateResult:
            session = await self._session(args)
            update = await session.pty_exec_start(
                *args.command,
                timeout=args.timeout,
                shell=args.shell,
                user=args.user,
                tty=args.tty,
                yield_time_s=args.yield_time_s,
                max_output_tokens=args.max_output_tokens,
            )
            return PtyExecUpdateResult(
                process_id=update.process_id,
                output=update.output,
                exit_code=update.exit_code,
                original_token_count=update.original_token_count,
            )

        @activity.defn(name=f"{prefix}-sandbox_session_pty_write_stdin")
        async def pty_write_stdin(args: PtyWriteStdinArgs) -> PtyExecUpdateResult:
            session = await self._session(args)
            update = await session.pty_write_stdin(
                session_id=args.session_id,
                chars=args.chars,
                yield_time_s=args.yield_time_s,
                max_output_tokens=args.max_output_tokens,
            )
            return PtyExecUpdateResult(
                process_id=update.process_id,
                output=update.output,
                exit_code=update.exit_code,
                original_token_count=update.original_token_count,
            )

        @activity.defn(name=f"{prefix}-sandbox_session_start")
        async def start(args: StartArgs) -> None:
            session = await self._session(args)
            await session.start()
            return None

        @activity.defn(name=f"{prefix}-sandbox_session_stop")
        async def session_stop(args: StopArgs) -> None:
            session = await self._session(args)
            await session.stop()
            return None

        @activity.defn(name=f"{prefix}-sandbox_session_shutdown")
        async def session_shutdown(args: StopArgs) -> None:
            key = str(args.state.session_id)
            session = self._sessions.get(key)
            if session is not None:
                await session.shutdown()
                del self._sessions[key]
            return None

        return [
            create_session,
            resume_session,
            delete_session,
            exec_,
            read,
            write,
            running,
            persist_workspace,
            hydrate_workspace,
            pty_exec_start,
            pty_write_stdin,
            start,
            session_stop,
            session_shutdown,
        ]
