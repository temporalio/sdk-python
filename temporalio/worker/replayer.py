"""Replayer."""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import json
import logging
import re
from dataclasses import dataclass
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    Iterable,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Type,
    Union,
)

import google.protobuf.json_format
from typing_extensions import TypedDict

import temporalio.api.history.v1
import temporalio.bridge.proto.workflow_activation
import temporalio.bridge.worker
import temporalio.converter
import temporalio.workflow
from temporalio.bridge.temporal_sdk_bridge import HistoryPusher

from .interceptor import Interceptor
from .worker import load_default_build_id
from .workflow import _WorkflowWorker
from .workflow_instance import UnsandboxedWorkflowRunner, WorkflowRunner

logger = logging.getLogger(__name__)


class Replayer:
    """Replayer to replay workflows from history."""

    def __init__(
        self,
        *,
        workflows: Sequence[Type],
        workflow_task_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None,
        workflow_runner: WorkflowRunner = UnsandboxedWorkflowRunner(),
        namespace: str = "ReplayNamespace",
        data_converter: temporalio.converter.DataConverter = temporalio.converter.default(),
        interceptors: Sequence[Interceptor] = [],
        build_id: Optional[str] = None,
        identity: Optional[str] = None,
        debug_mode: bool = False,
        fail_fast: bool = True,
    ) -> None:
        """Create a replayer to replay workflows from history.

        See :py:meth:`temporalio.worker.Worker.__init__` for a description of
        arguments. The same arguments need to be passed to the replayer that
        were passed to the worker when the workflow originally ran.
        """
        if not workflows:
            raise ValueError("At least one workflow must be specified")
        self._config = ReplayerConfig(
            workflows=list(workflows),
            workflow_task_executor=workflow_task_executor,
            workflow_runner=workflow_runner,
            namespace=namespace,
            data_converter=data_converter,
            interceptors=interceptors,
            build_id=build_id,
            identity=identity,
            debug_mode=debug_mode,
            fail_fast=fail_fast,
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
        history: WorkflowHistory,
    ) -> None:
        """Replay a workflow for the given history.

        Args:
            history: The history to replay. Can be fetched directly, or use
              :py:meth:`WorkflowHistory.from_json` to parse a history downloaded via `tctl` or the
              web ui.
        """

        async def gen_hist():
            yield history

        await self.replay_workflows(gen_hist())

    async def replay_workflows(
        self, histories: AsyncIterator[WorkflowHistory]
    ) -> WorkflowReplayResults:
        """Replay a workflow for the given history.

        Args:
            histories: The histories to replay, from an async iterator.
        """
        current_run_results = WorkflowReplayResults(dict())
        # Create bridge worker and workflow worker
        tq = f"replay-{self._config['build_id']}"
        (bridge_worker, pusher,) = temporalio.bridge.worker.Worker.for_replay(
            temporalio.bridge.worker.WorkerConfig(
                namespace=self._config["namespace"],
                task_queue=tq,
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
        workflow_worker = _WorkflowWorker(
            bridge_worker=lambda: bridge_worker,
            namespace=self._config["namespace"],
            task_queue=tq,
            workflows=self._config["workflows"],
            workflow_task_executor=self._config["workflow_task_executor"],
            workflow_runner=self._config["workflow_runner"],
            data_converter=self._config["data_converter"],
            interceptors=self._config["interceptors"],
            debug_mode=self._config["debug_mode"],
            on_eviction_hook=self._replayer_eviction_hook(
                fail_fast=self._config["fail_fast"],
                pusher=pusher,
                current_results=current_run_results,
            ),
        )

        async def history_feeder():
            try:
                async for history in histories:
                    # Extract workflow started event
                    started_event = next(
                        (
                            e
                            for e in history.events
                            if e.HasField("workflow_execution_started_event_attributes")
                        ),
                        None,
                    )
                    if not started_event:
                        raise ValueError("Started event not found")

                    as_history_proto = temporalio.api.history.v1.History(
                        events=history.events
                    )
                    await pusher.push_history(
                        history.workflow_id, as_history_proto.SerializeToString()
                    )
            finally:
                pusher.close()

        async def runner():
            # Run it
            try:
                await workflow_worker.run()
            finally:
                # We must finalize shutdown here
                try:
                    await bridge_worker.finalize_shutdown()
                except Exception:
                    logger.warning("Failed to finalize shutdown", exc_info=True)

        await asyncio.gather(history_feeder(), runner())
        return current_run_results

    @staticmethod
    def _replayer_eviction_hook(
        fail_fast: bool,
        pusher: HistoryPusher,
        current_results: WorkflowReplayResults,
    ) -> Callable[
        [str, temporalio.bridge.proto.workflow_activation.RemoveFromCache], None
    ]:
        def retfn(run_id, remove_job):
            ex = None
            ok_reasons = [
                temporalio.bridge.proto.workflow_activation.RemoveFromCache.EvictionReason.CACHE_FULL,
                temporalio.bridge.proto.workflow_activation.RemoveFromCache.EvictionReason.LANG_REQUESTED,
            ]
            if remove_job.reason in ok_reasons:
                # These reasons don't count as a failure-inducing eviction
                pass
            elif (
                remove_job.reason
                == temporalio.bridge.proto.workflow_activation.RemoveFromCache.EvictionReason.NONDETERMINISM
            ):
                ex = temporalio.workflow.NondeterminismError(remove_job.message)
            else:
                ex = RuntimeError(f"{remove_job.reason}: {remove_job.message}")

            if ex is not None:
                if fail_fast:
                    pusher.close()
                    raise ex
                else:
                    current_results.failure_details[run_id] = ex

        return retfn


class ReplayerConfig(TypedDict, total=False):
    """TypedDict of config originally passed to :py:class:`Replayer`."""

    workflows: Sequence[Type]
    workflow_task_executor: Optional[concurrent.futures.ThreadPoolExecutor]
    workflow_runner: WorkflowRunner
    namespace: str
    data_converter: temporalio.converter.DataConverter
    interceptors: Sequence[Interceptor]
    build_id: Optional[str]
    identity: Optional[str]
    debug_mode: bool
    fail_fast: bool


@dataclass
class WorkflowHistory:
    """A workflow's ID and history."""

    workflow_id: str
    events: Sequence[temporalio.api.history.v1.HistoryEvent]

    @classmethod
    def from_json(
        cls, workflow_id: str, history: Union[str, Dict[str, Any]]
    ) -> WorkflowHistory:
        """Construct a WorkflowHistory from an ID and a json dump of history.

        Args:
            workflow_id: The workflow's ID
            history: A string or parsed-to-dict representation of workflow history.
        """
        parsed = _history_from_json(history)
        return cls(workflow_id, parsed.events)


@dataclass
class WorkflowReplayResults:
    """Results of replaying multiple workflows."""

    failure_details: MutableMapping[str, Exception]

    def had_any_failure(self) -> bool:
        """Returns True if any run experienced a failure."""
        return len(self.failure_details) > 0


def _history_from_json(
    history: Union[str, Dict[str, Any]]
) -> temporalio.api.history.v1.History:
    if isinstance(history, str):
        history = json.loads(history)
    else:
        # Copy the dict so we can mutate it
        history = copy.deepcopy(history)
    if not isinstance(history, dict):
        raise ValueError("JSON history not a dictionary")
    events = history.get("events")
    if not isinstance(events, Iterable):
        raise ValueError("History does not have iterable 'events'")
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("Event not a dictionary")
        _fix_history_enum(
            "CANCEL_EXTERNAL_WORKFLOW_EXECUTION_FAILED_CAUSE",
            event,
            "requestCancelExternalWorkflowExecutionFailedEventAttributes",
            "cause",
        )
        _fix_history_enum("CONTINUE_AS_NEW_INITIATOR", event, "*", "initiator")
        _fix_history_enum("EVENT_TYPE", event, "eventType")
        _fix_history_enum(
            "PARENT_CLOSE_POLICY",
            event,
            "startChildWorkflowExecutionInitiatedEventAttributes",
            "parentClosePolicy",
        )
        _fix_history_enum("RETRY_STATE", event, "*", "retryState")
        _fix_history_enum(
            "SIGNAL_EXTERNAL_WORKFLOW_EXECUTION_FAILED_CAUSE",
            event,
            "signalExternalWorkflowExecutionFailedEventAttributes",
            "cause",
        )
        _fix_history_enum(
            "START_CHILD_WORKFLOW_EXECUTION_FAILED_CAUSE",
            event,
            "startChildWorkflowExecutionFailedEventAttributes",
            "cause",
        )
        _fix_history_enum("TASK_QUEUE_KIND", event, "*", "taskQueue", "kind")
        _fix_history_enum(
            "TIMEOUT_TYPE",
            event,
            "workflowTaskTimedOutEventAttributes",
            "timeoutType",
        )
        _fix_history_enum(
            "WORKFLOW_ID_REUSE_POLICY",
            event,
            "startChildWorkflowExecutionInitiatedEventAttributes",
            "workflowIdReusePolicy",
        )
        _fix_history_enum(
            "WORKFLOW_TASK_FAILED_CAUSE",
            event,
            "workflowTaskFailedEventAttributes",
            "cause",
        )
        _fix_history_failure(event, "*", "failure")
        _fix_history_failure(event, "activityTaskStartedEventAttributes", "lastFailure")
        _fix_history_failure(
            event, "workflowExecutionStartedEventAttributes", "continuedFailure"
        )
    return google.protobuf.json_format.ParseDict(
        history, temporalio.api.history.v1.History(), ignore_unknown_fields=True
    )


_pascal_case_match = re.compile("([A-Z]+)")


def _fix_history_failure(parent: Dict[str, Any], *attrs: str) -> None:
    _fix_history_enum(
        "TIMEOUT_TYPE", parent, *attrs, "timeoutFailureInfo", "timeoutType"
    )
    _fix_history_enum("RETRY_STATE", parent, *attrs, "*", "retryState")
    # Recurse into causes. First collect all failure parents.
    parents = [parent]
    for attr in attrs:
        new_parents = []
        for parent in parents:
            if attr == "*":
                for v in parent.values():
                    if isinstance(v, dict):
                        new_parents.append(v)
            else:
                child = parent.get(attr)
                if isinstance(child, dict):
                    new_parents.append(child)
        if not new_parents:
            return
        parents = new_parents
    # Fix each
    for parent in parents:
        _fix_history_failure(parent, "cause")


def _fix_history_enum(prefix: str, parent: Dict[str, Any], *attrs: str) -> None:
    # If the attr is "*", we need to handle all dict children
    if attrs[0] == "*":
        for child in parent.values():
            if isinstance(child, dict):
                _fix_history_enum(prefix, child, *attrs[1:])
    else:
        child = parent.get(attrs[0])
        if isinstance(child, str) and len(attrs) == 1:
            # We only fix it if it doesn't already have the prefix
            if not parent[attrs[0]].startswith(prefix):
                parent[attrs[0]] = (
                    prefix + _pascal_case_match.sub(r"_\1", child).upper()
                )
        elif isinstance(child, dict) and len(attrs) > 1:
            _fix_history_enum(prefix, child, *attrs[1:])
        elif isinstance(child, list) and len(attrs) > 1:
            for child_item in child:
                if isinstance(child_item, dict):
                    _fix_history_enum(prefix, child_item, *attrs[1:])
