"""Replayer."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Dict, Mapping, Optional, Sequence, Type

from typing_extensions import TypedDict

import temporalio.api.history.v1
import temporalio.bridge.proto.workflow_activation
import temporalio.bridge.worker
import temporalio.client
import temporalio.converter
import temporalio.runtime
import temporalio.workflow

from ._interceptor import Interceptor
from ._worker import load_default_build_id
from ._workflow import _WorkflowWorker
from ._workflow_instance import UnsandboxedWorkflowRunner, WorkflowRunner
from .workflow_sandbox import SandboxedWorkflowRunner

logger = logging.getLogger(__name__)


class Replayer:
    """Replayer to replay workflows from history."""

    def __init__(
        self,
        *,
        workflows: Sequence[Type],
        workflow_task_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None,
        workflow_runner: WorkflowRunner = SandboxedWorkflowRunner(),
        unsandboxed_workflow_runner: WorkflowRunner = UnsandboxedWorkflowRunner(),
        namespace: str = "ReplayNamespace",
        data_converter: temporalio.converter.DataConverter = temporalio.converter.DataConverter.default,
        interceptors: Sequence[Interceptor] = [],
        build_id: Optional[str] = None,
        identity: Optional[str] = None,
        debug_mode: bool = False,
        runtime: Optional[temporalio.runtime.Runtime] = None,
    ) -> None:
        """Create a replayer to replay workflows from history.

        See :py:meth:`temporalio.worker.Worker.__init__` for a description of
        most of the arguments. The same arguments need to be passed to the
        replayer that were passed to the worker when the workflow originally
        ran.
        """
        if not workflows:
            raise ValueError("At least one workflow must be specified")
        self._config = ReplayerConfig(
            workflows=list(workflows),
            workflow_task_executor=workflow_task_executor,
            workflow_runner=workflow_runner,
            unsandboxed_workflow_runner=unsandboxed_workflow_runner,
            namespace=namespace,
            data_converter=data_converter,
            interceptors=interceptors,
            build_id=build_id,
            identity=identity,
            debug_mode=debug_mode,
            runtime=runtime,
        )

    def config(self) -> ReplayerConfig:
        """Config, as a dictionary, used to create this replayer.

        Returns:
            Configuration, shallow-copied.
        """
        config = self._config.copy()
        config["workflows"] = list(config["workflows"])
        return config

    async def replay_workflow(
        self,
        history: temporalio.client.WorkflowHistory,
        *,
        raise_on_replay_failure: bool = True,
    ) -> WorkflowReplayResult:
        """Replay a workflow for the given history.

        Args:
            history: The history to replay. Can be fetched directly, or use
                :py:meth:`temporalio.client.WorkflowHistory.from_json` to parse
                a history downloaded via ``tctl`` or the web UI.
            raise_on_replay_failure: If ``True`` (the default), this will raise
                a :py:attr:`WorkflowReplayResult.replay_failure` if it is
                present.
        """

        async def history_iterator():
            yield history

        async with self.workflow_replay_iterator(history_iterator()) as replay_iterator:
            async for result in replay_iterator:
                if raise_on_replay_failure and result.replay_failure:
                    raise result.replay_failure
                return result
            # Should never be reached
            raise RuntimeError("No histories")

    async def replay_workflows(
        self,
        histories: AsyncIterator[temporalio.client.WorkflowHistory],
        *,
        raise_on_replay_failure: bool = True,
    ) -> WorkflowReplayResults:
        """Replay workflows for the given histories.

        This is a shortcut for :py:meth:`workflow_replay_iterator` that iterates
        all results and aggregates information about them.

        Args:
            histories: The histories to replay, from an async iterator.
            raise_on_replay_failure: If ``True`` (the default), this will raise
                the first replay failure seen.

        Returns:
            Aggregated results.
        """
        async with self.workflow_replay_iterator(histories) as replay_iterator:
            replay_failures: Dict[str, Exception] = {}
            async for result in replay_iterator:
                if result.replay_failure:
                    if raise_on_replay_failure:
                        raise result.replay_failure
                    replay_failures[result.history.run_id] = result.replay_failure
            return WorkflowReplayResults(replay_failures=replay_failures)

    @asynccontextmanager
    async def workflow_replay_iterator(
        self, histories: AsyncIterator[temporalio.client.WorkflowHistory]
    ) -> AsyncIterator[AsyncIterator[WorkflowReplayResult]]:
        """Replay workflows for the given histories.

        This is a context manager for use via ``async with``. The value is an
        iterator for use via ``async for``.

        Args:
            histories: The histories to replay, from an async iterator.

        Returns:
            An async iterator that returns replayed workflow results as they are
            replayed.
        """
        # Create bridge worker
        task_queue = f"replay-{self._config['build_id']}"
        runtime = self._config["runtime"] or temporalio.runtime.Runtime.default()
        bridge_worker, pusher = temporalio.bridge.worker.Worker.for_replay(
            runtime._core_runtime,
            temporalio.bridge.worker.WorkerConfig(
                namespace=self._config["namespace"],
                task_queue=task_queue,
                build_id=self._config["build_id"] or load_default_build_id(),
                identity_override=self._config["identity"],
                # All values below are ignored but required by Core
                max_cached_workflows=1,
                max_outstanding_workflow_tasks=1,
                max_outstanding_activities=1,
                max_outstanding_local_activities=1,
                max_concurrent_workflow_task_polls=1,
                nonsticky_to_sticky_poll_ratio=1,
                max_concurrent_activity_task_polls=1,
                no_remote_activities=True,
                sticky_queue_schedule_to_start_timeout_millis=1000,
                max_heartbeat_throttle_interval_millis=1000,
                default_heartbeat_throttle_interval_millis=1000,
                max_activities_per_second=None,
                max_task_queue_activities_per_second=None,
            ),
        )

        try:
            last_replay_failure: Optional[Exception]
            last_replay_complete = asyncio.Event()

            # Create eviction hook
            def on_eviction_hook(
                run_id: str,
                remove_job: temporalio.bridge.proto.workflow_activation.RemoveFromCache,
            ) -> None:
                nonlocal last_replay_failure
                if (
                    remove_job.reason
                    == temporalio.bridge.proto.workflow_activation.RemoveFromCache.EvictionReason.NONDETERMINISM
                ):
                    last_replay_failure = temporalio.workflow.NondeterminismError(
                        remove_job.message
                    )
                elif (
                    remove_job.reason
                    != temporalio.bridge.proto.workflow_activation.RemoveFromCache.EvictionReason.CACHE_FULL
                    and remove_job.reason
                    != temporalio.bridge.proto.workflow_activation.RemoveFromCache.EvictionReason.LANG_REQUESTED
                ):
                    last_replay_failure = RuntimeError(
                        f"{remove_job.reason}: {remove_job.message}"
                    )
                else:
                    last_replay_failure = None
                last_replay_complete.set()

            # Start the worker
            workflow_worker_task = asyncio.create_task(
                _WorkflowWorker(
                    bridge_worker=lambda: bridge_worker,
                    namespace=self._config["namespace"],
                    task_queue=task_queue,
                    workflows=self._config["workflows"],
                    workflow_task_executor=self._config["workflow_task_executor"],
                    workflow_runner=self._config["workflow_runner"],
                    unsandboxed_workflow_runner=self._config[
                        "unsandboxed_workflow_runner"
                    ],
                    data_converter=self._config["data_converter"],
                    interceptors=self._config["interceptors"],
                    debug_mode=self._config["debug_mode"],
                    on_eviction_hook=on_eviction_hook,
                    disable_eager_activity_execution=False,
                ).run()
            )

            # Yield iterator
            async def replay_iterator() -> AsyncIterator[WorkflowReplayResult]:
                async for history in histories:
                    # Clear last complete and push history
                    last_replay_complete.clear()
                    await pusher.push_history(
                        history.workflow_id,
                        temporalio.api.history.v1.History(
                            events=history.events
                        ).SerializeToString(),
                    )

                    # Wait for worker error or last replay to complete. This
                    # should never take more than a few seconds due to deadlock
                    # detector but we cannot add timeout just in case debug mode
                    # is enabled.
                    await asyncio.wait(  # type: ignore
                        [
                            workflow_worker_task,
                            asyncio.create_task(last_replay_complete.wait()),
                        ],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    # If worker task complete, wait on it so it'll throw
                    if workflow_worker_task.done():
                        await workflow_worker_task
                    # Should always be set if workflow worker didn't throw
                    assert last_replay_complete.is_set()

                    yield WorkflowReplayResult(
                        history=history,
                        replay_failure=last_replay_failure,
                    )

            yield replay_iterator()
        finally:
            # Close the pusher
            pusher.close()
            # If the workflow worker task is not done, wait for it
            try:
                if not workflow_worker_task.done():
                    await workflow_worker_task
            except Exception:
                logger.warning("Failed to shutdown worker", exc_info=True)
            finally:
                # We must finalize shutdown here
                try:
                    await bridge_worker.finalize_shutdown()
                except Exception:
                    logger.warning("Failed to finalize shutdown", exc_info=True)


class ReplayerConfig(TypedDict, total=False):
    """TypedDict of config originally passed to :py:class:`Replayer`."""

    workflows: Sequence[Type]
    workflow_task_executor: Optional[concurrent.futures.ThreadPoolExecutor]
    workflow_runner: WorkflowRunner
    unsandboxed_workflow_runner: WorkflowRunner
    namespace: str
    data_converter: temporalio.converter.DataConverter
    interceptors: Sequence[Interceptor]
    build_id: Optional[str]
    identity: Optional[str]
    debug_mode: bool
    runtime: Optional[temporalio.runtime.Runtime]


@dataclass(frozen=True)
class WorkflowReplayResult:
    """Single workflow replay result."""

    history: temporalio.client.WorkflowHistory
    """History originally passed for this workflow replay."""

    replay_failure: Optional[Exception]
    """Failure during replay if any.
    
    This does not mean your workflow exited by raising an error, but rather that
    some task failure such as
    :py:class:`temporalio.workflow.NondeterminismError` was encountered during
    replay - likely indicating your workflow code is incompatible with the
    history.
    """


@dataclass(frozen=True)
class WorkflowReplayResults:
    """Results of replaying multiple workflows."""

    replay_failures: Mapping[str, Exception]
    """Replay failures, keyed by run ID."""
