"""LangSmith interceptor implementation for Temporal SDK."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, NoReturn

import temporalio.activity
import temporalio.client
import temporalio.converter
import temporalio.worker
import temporalio.workflow
from langsmith import tracing_context
from langsmith.run_helpers import get_current_run_tree
from langsmith.run_trees import RunTree
from temporalio.api.common.v1 import Payload
from temporalio.exceptions import ApplicationError, ApplicationErrorCategory

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HEADER_KEY = "_langsmith-context"

# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------

_payload_converter = temporalio.converter.PayloadConverter.default


def _inject_context(
    headers: Mapping[str, Payload],
    run_tree: Any,
) -> dict[str, Payload]:
    """Inject LangSmith context into Temporal payload headers.

    Serializes the run's trace context (trace ID, parent run ID, dotted order)
    into a Temporal header under ``_langsmith-context``, enabling parent-child
    trace nesting across process boundaries (client → worker, workflow → activity).
    """
    ls_headers = run_tree.to_headers()
    return {
        **headers,
        HEADER_KEY: _payload_converter.to_payloads([ls_headers])[0],
    }


def _inject_current_context(
    headers: Mapping[str, Payload],
) -> Mapping[str, Payload]:
    """Inject the current ambient LangSmith context into Temporal payload headers.

    Reads ``get_current_run_tree()`` and injects if present. Returns headers
    unchanged if no context is active. Called unconditionally so that context
    propagation is independent of the ``add_temporal_runs`` toggle.
    """
    current = get_current_run_tree()
    if current is not None:
        return _inject_context(headers, current)
    return headers


def _extract_context(
    headers: Mapping[str, Payload],
) -> Any | None:
    """Extract LangSmith context from Temporal payload headers.

    Reconstructs a :class:`RunTree` from the ``_langsmith-context`` header on
    the receiving side, so inbound interceptors can establish a parent-child
    relationship with the sender's run. Returns ``None`` if no header is present.
    """
    header = headers.get(HEADER_KEY)
    if not header:
        return None
    ls_headers = _payload_converter.from_payloads([header])[0]
    return RunTree.from_headers(ls_headers)


def _inject_nexus_context(
    headers: dict[str, str],
    run_tree: Any,
) -> dict[str, str]:
    """Inject LangSmith context into Nexus string headers."""
    ls_headers = run_tree.to_headers()
    return {
        **headers,
        HEADER_KEY: json.dumps(ls_headers),
    }


def _extract_nexus_context(
    headers: dict[str, str],
) -> Any | None:
    """Extract LangSmith context from Nexus string headers."""
    raw = headers.get(HEADER_KEY)
    if not raw:
        return None
    ls_headers = json.loads(raw)
    return RunTree.from_headers(ls_headers)


# ---------------------------------------------------------------------------
# Replay safety
# ---------------------------------------------------------------------------


def _is_replaying() -> bool:
    """Check if we're currently replaying workflow history."""
    return (
        temporalio.workflow.in_workflow()
        and temporalio.workflow.unsafe.is_replaying_history_events()
    )


# ---------------------------------------------------------------------------
# Sandbox-safe post/patch helpers
# ---------------------------------------------------------------------------


def _safe_post(run_tree: Any, in_workflow: bool) -> None:
    if in_workflow:
        with temporalio.workflow.unsafe.sandbox_unrestricted():
            run_tree.post()
    else:
        run_tree.post()


def _safe_patch(run_tree: Any, in_workflow: bool) -> None:
    if in_workflow:
        with temporalio.workflow.unsafe.sandbox_unrestricted():
            run_tree.patch()
    else:
        run_tree.patch()


# ---------------------------------------------------------------------------
# _maybe_run context manager
# ---------------------------------------------------------------------------


def _is_benign_error(exc: Exception) -> bool:
    """Check if an exception is a benign ApplicationError."""
    return (
        isinstance(exc, ApplicationError)
        and getattr(exc, "category", None) == ApplicationErrorCategory.BENIGN
    )


@contextmanager
def _maybe_run(
    client: Any,
    name: str,
    *,
    add_temporal_runs: bool,
    run_type: str = "chain",
    inputs: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    parent: Any | None = None,
    project_name: str | None = None,
    in_workflow: bool = False,
) -> Iterator[Any | None]:
    """Create a LangSmith run, handling errors and replay.

    - If replaying, yields None (skip tracing entirely).
    - If add_temporal_runs is False, yields None (no run created).
      Context propagation is handled unconditionally by callers.
    - When a run IS created, sets it as ambient context via
      ``tracing_context(parent=run_tree)`` so ``get_current_run_tree()``
      returns it and ``_inject_current_context()`` can inject it.
    - On exception: marks run as errored (unless benign ApplicationError), re-raises.
    - If in_workflow is True, wraps post()/patch() in sandbox_unrestricted().
    """
    if _is_replaying():
        yield None
        return

    if not add_temporal_runs:
        yield None
        return

    # If no explicit parent, inherit from ambient @traceable context
    if parent is None:
        parent = get_current_run_tree()

    kwargs: dict[str, Any] = dict(
        name=name,
        run_type=run_type,
        inputs=inputs or {},
        ls_client=client,
    )
    if project_name is not None:
        kwargs["project_name"] = project_name
    if parent is not None:
        kwargs["parent_run"] = parent
    if metadata:
        kwargs["extra"] = {"metadata": metadata}
    if tags:
        kwargs["tags"] = tags
    run_tree = RunTree(**kwargs)
    _safe_post(run_tree, in_workflow)
    try:
        with tracing_context(parent=run_tree, client=client):
            yield run_tree
    except Exception as exc:
        if not _is_benign_error(exc):
            run_tree.end(error=f"{type(exc).__name__}: {exc}")
            _safe_patch(run_tree, in_workflow)
        raise
    else:
        run_tree.end(outputs={"status": "ok"})
        _safe_patch(run_tree, in_workflow)


# ---------------------------------------------------------------------------
# LangSmithInterceptor
# ---------------------------------------------------------------------------


class LangSmithInterceptor(
    temporalio.client.Interceptor, temporalio.worker.Interceptor
):
    """Interceptor that supports client and worker LangSmith run creation
    and context propagation.
    """

    def __init__(
        self,
        *,
        client: Any | None = None,
        project_name: str | None = None,
        add_temporal_runs: bool = True,
        default_metadata: dict[str, Any] | None = None,
        default_tags: list[str] | None = None,
    ) -> None:
        # Import langsmith.Client lazily to avoid hard dependency at import time
        if client is None:
            import langsmith

            client = langsmith.Client()
        self._client = client
        self._project_name = project_name
        self._add_temporal_runs = add_temporal_runs
        self._default_metadata = default_metadata or {}
        self._default_tags = default_tags or []

    def intercept_client(
        self, next: temporalio.client.OutboundInterceptor
    ) -> temporalio.client.OutboundInterceptor:
        return _LangSmithClientOutboundInterceptor(next, self)

    def intercept_activity(
        self, next: temporalio.worker.ActivityInboundInterceptor
    ) -> temporalio.worker.ActivityInboundInterceptor:
        return _LangSmithActivityInboundInterceptor(next, self)

    def workflow_interceptor_class(
        self, input: temporalio.worker.WorkflowInterceptorClassInput
    ) -> type[_LangSmithWorkflowInboundInterceptor]:
        config = self

        class InterceptorWithConfig(_LangSmithWorkflowInboundInterceptor):
            _config = config

        return InterceptorWithConfig

    def intercept_nexus_operation(
        self, next: temporalio.worker.NexusOperationInboundInterceptor
    ) -> temporalio.worker.NexusOperationInboundInterceptor:
        return _LangSmithNexusOperationInboundInterceptor(next, self)


# ---------------------------------------------------------------------------
# Client Outbound Interceptor
# ---------------------------------------------------------------------------


class _LangSmithClientOutboundInterceptor(temporalio.client.OutboundInterceptor):
    """Instruments all client-side calls with LangSmith runs."""

    def __init__(
        self,
        next: temporalio.client.OutboundInterceptor,
        config: LangSmithInterceptor,
    ) -> None:
        super().__init__(next)
        self._config = config

    async def start_workflow(self, input: Any) -> Any:
        prefix = (
            "SignalWithStartWorkflow" if input.start_signal else "StartWorkflow"
        )
        with _maybe_run(
            self._config._client,
            f"{prefix}:{input.workflow}",
            add_temporal_runs=self._config._add_temporal_runs,
            metadata={**self._config._default_metadata},
            tags=list(self._config._default_tags),
            project_name=self._config._project_name,
        ):
            input.headers = _inject_current_context(input.headers)
            return await super().start_workflow(input)

    async def query_workflow(self, input: Any) -> Any:
        with _maybe_run(
            self._config._client,
            f"QueryWorkflow:{input.query}",
            add_temporal_runs=self._config._add_temporal_runs,
            metadata={**self._config._default_metadata},
            tags=list(self._config._default_tags),
            project_name=self._config._project_name,
        ):
            input.headers = _inject_current_context(input.headers)
            return await super().query_workflow(input)

    async def signal_workflow(self, input: Any) -> None:
        with _maybe_run(
            self._config._client,
            f"SignalWorkflow:{input.signal}",
            add_temporal_runs=self._config._add_temporal_runs,
            metadata={**self._config._default_metadata},
            tags=list(self._config._default_tags),
            project_name=self._config._project_name,
        ):
            input.headers = _inject_current_context(input.headers)
            return await super().signal_workflow(input)

    async def start_workflow_update(self, input: Any) -> Any:
        with _maybe_run(
            self._config._client,
            f"StartWorkflowUpdate:{input.update}",
            add_temporal_runs=self._config._add_temporal_runs,
            metadata={**self._config._default_metadata},
            tags=list(self._config._default_tags),
            project_name=self._config._project_name,
        ):
            input.headers = _inject_current_context(input.headers)
            return await super().start_workflow_update(input)

    async def start_update_with_start_workflow(self, input: Any) -> Any:
        with _maybe_run(
            self._config._client,
            f"StartUpdateWithStartWorkflow:{input.start_workflow_input.workflow}",
            add_temporal_runs=self._config._add_temporal_runs,
            metadata={**self._config._default_metadata},
            tags=list(self._config._default_tags),
            project_name=self._config._project_name,
        ):
            input.start_workflow_input.headers = _inject_current_context(
                input.start_workflow_input.headers
            )
            input.update_workflow_input.headers = _inject_current_context(
                input.update_workflow_input.headers
            )
            return await super().start_update_with_start_workflow(input)


# ---------------------------------------------------------------------------
# Activity Inbound Interceptor
# ---------------------------------------------------------------------------


class _LangSmithActivityInboundInterceptor(
    temporalio.worker.ActivityInboundInterceptor
):
    """Instruments activity execution with LangSmith runs."""

    def __init__(
        self,
        next: temporalio.worker.ActivityInboundInterceptor,
        config: LangSmithInterceptor,
    ) -> None:
        super().__init__(next)
        self._config = config

    async def execute_activity(self, input: Any) -> Any:
        parent = _extract_context(input.headers)
        info = temporalio.activity.info()
        metadata = {
            **self._config._default_metadata,
            "temporalWorkflowID": info.workflow_id or "",
            "temporalRunID": info.workflow_run_id or "",
            "temporalActivityID": info.activity_id,
        }
        # Unconditionally set tracing context so @traceable functions inside
        # activities can use the plugin's LangSmith client and inherit parent.
        # When add_temporal_runs=True: _maybe_run overrides with the RunActivity run.
        # When add_temporal_runs=False: parent (if any) remains active for @traceable,
        # and the client is available even without a parent.
        # Override the parent's ls_client so @traceable children (via create_child)
        # use the plugin's client rather than lazily creating a real one.
        if parent is not None and hasattr(parent, "ls_client"):
            parent.ls_client = self._config._client
        ctx_kwargs: dict[str, Any] = {
            "client": self._config._client,
            "enabled": True,
        }
        if parent:
            ctx_kwargs["parent"] = parent
        with tracing_context(**ctx_kwargs):
            with _maybe_run(
                self._config._client,
                f"RunActivity:{info.activity_type}",
                add_temporal_runs=self._config._add_temporal_runs,
                run_type="tool",
                metadata=metadata,
                tags=list(self._config._default_tags),
                parent=parent,
                project_name=self._config._project_name,
            ):
                return await super().execute_activity(input)


# ---------------------------------------------------------------------------
# Workflow Inbound Interceptor
# ---------------------------------------------------------------------------


class _LangSmithWorkflowInboundInterceptor(
    temporalio.worker.WorkflowInboundInterceptor
):
    """Instruments workflow execution with LangSmith runs."""

    _config: LangSmithInterceptor
    _current_run: Any | None = None

    def init(self, outbound: temporalio.worker.WorkflowOutboundInterceptor) -> None:
        super().init(
            _LangSmithWorkflowOutboundInterceptor(outbound, self._config, self)
        )

    @contextmanager
    def _workflow_maybe_run(
        self, name: str, parent: Any | None = None
    ) -> Iterator[Any | None]:
        """Workflow-specific run creation with metadata.

        Stores the run (or parent fallback) as ``_current_run`` so the outbound
        interceptor can propagate context even when ``add_temporal_runs=False``.
        """
        info = temporalio.workflow.info()
        metadata = {
            **self._config._default_metadata,
            "temporalWorkflowID": info.workflow_id,
            "temporalRunID": info.run_id,
        }
        with _maybe_run(
            self._config._client,
            name,
            add_temporal_runs=self._config._add_temporal_runs,
            metadata=metadata,
            tags=list(self._config._default_tags),
            parent=parent,
            project_name=self._config._project_name,
            in_workflow=True,
        ) as run:
            self._current_run = run or parent
            try:
                yield run
            finally:
                self._current_run = None

    async def execute_workflow(self, input: Any) -> Any:
        parent = _extract_context(input.headers)
        with self._workflow_maybe_run(
            f"RunWorkflow:{temporalio.workflow.info().workflow_type}",
            parent=parent,
        ):
            return await super().execute_workflow(input)

    async def handle_signal(self, input: Any) -> None:
        parent = _extract_context(input.headers)
        with self._workflow_maybe_run(
            f"HandleSignal:{input.signal}", parent=parent
        ):
            return await super().handle_signal(input)

    async def handle_query(self, input: Any) -> Any:
        parent = _extract_context(input.headers)
        with self._workflow_maybe_run(
            f"HandleQuery:{input.query}", parent=parent
        ):
            return await super().handle_query(input)

    def handle_update_validator(self, input: Any) -> None:
        parent = _extract_context(input.headers)
        with self._workflow_maybe_run(
            f"ValidateUpdate:{input.update}", parent=parent
        ):
            return super().handle_update_validator(input)

    async def handle_update_handler(self, input: Any) -> Any:
        parent = _extract_context(input.headers)
        with self._workflow_maybe_run(
            f"HandleUpdate:{input.update}", parent=parent
        ):
            return await super().handle_update_handler(input)


# ---------------------------------------------------------------------------
# Workflow Outbound Interceptor
# ---------------------------------------------------------------------------


class _LangSmithWorkflowOutboundInterceptor(
    temporalio.worker.WorkflowOutboundInterceptor
):
    """Instruments all outbound calls from workflow code."""

    def __init__(
        self,
        next: temporalio.worker.WorkflowOutboundInterceptor,
        config: LangSmithInterceptor,
        inbound: _LangSmithWorkflowInboundInterceptor,
    ) -> None:
        super().__init__(next)
        self._config = config
        self._inbound = inbound

    @contextmanager
    def _workflow_maybe_run(self, name: str) -> Iterator[Any | None]:
        """Outbound workflow run creation, parented under inbound's current run."""
        with _maybe_run(
            self._config._client,
            name,
            add_temporal_runs=self._config._add_temporal_runs,
            metadata={**self._config._default_metadata},
            tags=list(self._config._default_tags),
            parent=self._inbound._current_run,
            project_name=self._config._project_name,
            in_workflow=True,
        ) as run:
            yield run

    def start_activity(self, input: Any) -> Any:
        with self._workflow_maybe_run(f"StartActivity:{input.activity}") as run:
            context_source = run or self._inbound._current_run
            if context_source:
                input.headers = _inject_context(input.headers, context_source)
            return super().start_activity(input)

    def start_local_activity(self, input: Any) -> Any:
        with self._workflow_maybe_run(f"StartActivity:{input.activity}") as run:
            context_source = run or self._inbound._current_run
            if context_source:
                input.headers = _inject_context(input.headers, context_source)
            return super().start_local_activity(input)

    async def start_child_workflow(self, input: Any) -> Any:
        with self._workflow_maybe_run(
            f"StartChildWorkflow:{input.workflow}"
        ) as run:
            context_source = run or self._inbound._current_run
            if context_source:
                input.headers = _inject_context(input.headers, context_source)
            return await super().start_child_workflow(input)

    async def signal_child_workflow(self, input: Any) -> None:
        with self._workflow_maybe_run(
            f"SignalChildWorkflow:{input.signal}"
        ) as run:
            context_source = run or self._inbound._current_run
            if context_source:
                input.headers = _inject_context(input.headers, context_source)
            return await super().signal_child_workflow(input)

    async def signal_external_workflow(self, input: Any) -> None:
        with self._workflow_maybe_run(
            f"SignalExternalWorkflow:{input.signal}"
        ) as run:
            context_source = run or self._inbound._current_run
            if context_source:
                input.headers = _inject_context(input.headers, context_source)
            return await super().signal_external_workflow(input)

    def continue_as_new(self, input: Any) -> NoReturn:
        # No trace created, but inject context from inbound's current run
        current_run = getattr(self._inbound, "_current_run", None)
        if current_run:
            input.headers = _inject_context(input.headers, current_run)
        super().continue_as_new(input)

    async def start_nexus_operation(self, input: Any) -> Any:
        with self._workflow_maybe_run(
            f"StartNexusOperation:{input.service}/{input.operation_name}"
        ) as run:
            context_source = run or self._inbound._current_run
            if context_source:
                input.headers = _inject_nexus_context(
                    input.headers or {}, context_source
                )
            return await super().start_nexus_operation(input)


# ---------------------------------------------------------------------------
# Nexus Operation Inbound Interceptor
# ---------------------------------------------------------------------------


class _LangSmithNexusOperationInboundInterceptor(
    temporalio.worker.NexusOperationInboundInterceptor
):
    """Instruments Nexus operations with LangSmith runs."""

    def __init__(
        self,
        next: temporalio.worker.NexusOperationInboundInterceptor,
        config: LangSmithInterceptor,
    ) -> None:
        super().__init__(next)
        self._config = config

    async def execute_nexus_operation_start(self, input: Any) -> Any:
        parent = _extract_nexus_context(input.ctx.headers)
        with _maybe_run(
            self._config._client,
            f"RunStartNexusOperationHandler:{input.ctx.service}/{input.ctx.operation}",
            add_temporal_runs=self._config._add_temporal_runs,
            run_type="tool",
            metadata={**self._config._default_metadata},
            tags=list(self._config._default_tags),
            parent=parent,
            project_name=self._config._project_name,
        ):
            return await self.next.execute_nexus_operation_start(input)

    async def execute_nexus_operation_cancel(self, input: Any) -> Any:
        parent = _extract_nexus_context(input.ctx.headers)
        with _maybe_run(
            self._config._client,
            f"RunCancelNexusOperationHandler:{input.ctx.service}/{input.ctx.operation}",
            add_temporal_runs=self._config._add_temporal_runs,
            run_type="tool",
            metadata={**self._config._default_metadata},
            tags=list(self._config._default_tags),
            parent=parent,
            project_name=self._config._project_name,
        ):
            return await self.next.execute_nexus_operation_cancel(input)
