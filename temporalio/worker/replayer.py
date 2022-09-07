"""Replayer."""

from __future__ import annotations

import concurrent.futures
import copy
import json
import logging
import re
from typing import Any, Dict, Iterable, Optional, Sequence, Type, Union

import google.protobuf.json_format
from typing_extensions import TypedDict

import temporalio.api.history.v1
import temporalio.bridge.worker
import temporalio.converter

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
        history: Union[temporalio.api.history.v1.History, str, Dict[str, Any]],
    ) -> None:
        """Replay a workflow for the given history.

        Args:
            history: The history to replay. Can be a proto history object or
                JSON history as exported via web/tctl. If JSON history, can be a
                JSON string or a JSON dictionary as returned by
                :py:func:`json.load`.
        """
        # Convert history if JSON or dict
        if not isinstance(history, temporalio.api.history.v1.History):
            history = _history_from_json(history)

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

        # Create bridge worker and workflow worker
        bridge_worker = temporalio.bridge.worker.Worker.for_replay(
            history,
            temporalio.bridge.worker.WorkerConfig(
                namespace=self._config["namespace"],
                task_queue=started_event.workflow_execution_started_event_attributes.task_queue.name,
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
            task_queue=started_event.workflow_execution_started_event_attributes.task_queue.name,
            workflows=self._config["workflows"],
            workflow_task_executor=self._config["workflow_task_executor"],
            workflow_runner=self._config["workflow_runner"],
            data_converter=self._config["data_converter"],
            interceptors=self._config["interceptors"],
            debug_mode=self._config["debug_mode"],
            fail_on_eviction=True,
        )

        # Run it
        try:
            await workflow_worker.run()
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
    namespace: str
    data_converter: temporalio.converter.DataConverter
    interceptors: Sequence[Interceptor]
    build_id: Optional[str]
    identity: Optional[str]
    debug_mode: bool


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
            "TIMEOUT_TYPE", event, "workflowTaskTimedOutEventAttributes", "timeoutType"
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
