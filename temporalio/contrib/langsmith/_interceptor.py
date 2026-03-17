"""LangSmith interceptor implementation for Temporal SDK."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, NoReturn

from langsmith import tracing_context
from langsmith.run_helpers import get_current_run_tree
from langsmith.run_trees import RunTree

import temporalio.activity
import temporalio.client
import temporalio.converter
import temporalio.worker
import temporalio.workflow
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


def _get_current_run_safe() -> ReplaySafeRunTree | None:
    """Get the current ambient LangSmith run tree, wrapped for replay safety."""
    raw = get_current_run_tree()
    return ReplaySafeRunTree(raw) if raw is not None else None


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
    return ReplaySafeRunTree(RunTree.from_headers(ls_headers))


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
    return ReplaySafeRunTree(RunTree.from_headers(ls_headers))


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
# ReplaySafeRunTree wrapper
# ---------------------------------------------------------------------------


class ReplaySafeRunTree:
    """Wraps a RunTree to handle replay skipping and sandbox safety transparently.

    During replay, ``post()``, ``end()``, and ``patch()`` become no-ops.
    Inside a workflow sandbox, ``post()`` and ``patch()`` are wrapped in
    ``sandbox_unrestricted()``.
    """

    def __init__(self, run_tree: Any) -> None:
        """Initialize with the underlying RunTree to wrap."""
        self._run = run_tree

    def to_headers(self) -> dict[str, str]:
        """Delegate header serialization to the underlying RunTree."""
        return self._run.to_headers()

    @property
    def ls_client(self) -> Any:
        """Get the LangSmith client from the underlying RunTree."""
        return self._run.ls_client

    @ls_client.setter
    def ls_client(self, value: Any) -> None:
        """Set the LangSmith client on the underlying RunTree."""
        self._run.ls_client = value

    def post(self) -> None:
        """Post the run to LangSmith, skipping during replay."""
        if _is_replaying():
            return
        if temporalio.workflow.in_workflow():
            with temporalio.workflow.unsafe.sandbox_unrestricted():
                self._run.post()
        else:
            self._run.post()

    def end(self, **kwargs: Any) -> None:
        """End the run, skipping during replay."""
        if _is_replaying():
            return
        self._run.end(**kwargs)

    def patch(self) -> None:
        """Patch the run to LangSmith, skipping during replay."""
        if _is_replaying():
            return
        if temporalio.workflow.in_workflow():
            with temporalio.workflow.unsafe.sandbox_unrestricted():
                self._run.patch()
        else:
            self._run.patch()


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
) -> Iterator[Any | None]:
    """Create a LangSmith run, handling errors.

    - If add_temporal_runs is False, yields None (no run created).
      Context propagation is handled unconditionally by callers.
    - When a run IS created, wraps it in :class:`ReplaySafeRunTree` for
      replay and sandbox safety, then sets it as ambient context via
      ``tracing_context(parent=raw_run)`` so ``get_current_run_tree()``
      returns it and ``_inject_current_context()`` can inject it.
    - On exception: marks run as errored (unless benign ApplicationError), re-raises.
    """
    if not add_temporal_runs:
        yield None
        return

    # If no explicit parent, inherit from ambient @traceable context
    if parent is None:
        parent = _get_current_run_safe()

    kwargs: dict[str, Any] = dict(
        name=name,
        run_type=run_type,
        inputs=inputs or {},
        ls_client=client,
    )
    if project_name is not None:
        kwargs["project_name"] = project_name
    if parent is not None:
        kwargs["parent_run"] = parent._run
    if metadata:
        kwargs["extra"] = {"metadata": metadata}
    if tags:
        kwargs["tags"] = tags
    raw_run = RunTree(**kwargs)
    run_tree = ReplaySafeRunTree(raw_run)
    run_tree.post()
    try:
        with tracing_context(parent=raw_run, client=client):
            yield run_tree
    except Exception as exc:
        if not _is_benign_error(exc):
            run_tree.end(error=f"{type(exc).__name__}: {exc}")
            run_tree.patch()
        raise
    else:
        run_tree.end(outputs={"status": "ok"})
        run_tree.patch()


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
        """Initialize the LangSmith interceptor with tracing configuration."""
        # Import langsmith.Client lazily to avoid hard dependency at import time
        if client is None:
            import langsmith

            client = langsmith.Client()
        self._client = client
        self._project_name = project_name
        self._add_temporal_runs = add_temporal_runs
        self._default_metadata = default_metadata or {}
        self._default_tags = default_tags or []

    @contextmanager
    def maybe_run(
        self,
        name: str,
        *,
        run_type: str = "chain",
        parent: Any | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> Iterator[Any | None]:
        """Create a LangSmith run with this interceptor's config already applied."""
        metadata = {**self._default_metadata, **(extra_metadata or {})}
        with _maybe_run(
            self._client,
            name,
            add_temporal_runs=self._add_temporal_runs,
            run_type=run_type,
            metadata=metadata,
            tags=list(self._default_tags),
            parent=parent,
            project_name=self._project_name,
        ) as run:
            yield run

    def intercept_client(
        self, next: temporalio.client.OutboundInterceptor
    ) -> temporalio.client.OutboundInterceptor:
        """Create a client outbound interceptor for LangSmith tracing."""
        return _LangSmithClientOutboundInterceptor(next, self)

    def intercept_activity(
        self, next: temporalio.worker.ActivityInboundInterceptor
    ) -> temporalio.worker.ActivityInboundInterceptor:
        """Create an activity inbound interceptor for LangSmith tracing."""
        return _LangSmithActivityInboundInterceptor(next, self)

    def workflow_interceptor_class(
        self, input: temporalio.worker.WorkflowInterceptorClassInput
    ) -> type[_LangSmithWorkflowInboundInterceptor]:
        """Return the workflow interceptor class with config bound."""
        config = self

        class InterceptorWithConfig(_LangSmithWorkflowInboundInterceptor):
            _config = config

        return InterceptorWithConfig

    def intercept_nexus_operation(
        self, next: temporalio.worker.NexusOperationInboundInterceptor
    ) -> temporalio.worker.NexusOperationInboundInterceptor:
        """Create a Nexus operation inbound interceptor for LangSmith tracing."""
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

    @contextmanager
    def _traced_call(self, name: str, input: Any) -> Iterator[None]:
        """Wrap a client call with a LangSmith run and inject context into headers."""
        with self._config.maybe_run(name):
            input.headers = _inject_current_context(input.headers)
            yield

    async def start_workflow(self, input: Any) -> Any:
        prefix = "SignalWithStartWorkflow" if input.start_signal else "StartWorkflow"
        with self._traced_call(f"{prefix}:{input.workflow}", input):
            return await super().start_workflow(input)

    async def query_workflow(self, input: Any) -> Any:
        with self._traced_call(f"QueryWorkflow:{input.query}", input):
            return await super().query_workflow(input)

    async def signal_workflow(self, input: Any) -> None:
        with self._traced_call(f"SignalWorkflow:{input.signal}", input):
            return await super().signal_workflow(input)

    async def start_workflow_update(self, input: Any) -> Any:
        with self._traced_call(f"StartWorkflowUpdate:{input.update}", input):
            return await super().start_workflow_update(input)

    async def start_update_with_start_workflow(self, input: Any) -> Any:
        with self._config.maybe_run(
            f"StartUpdateWithStartWorkflow:{input.start_workflow_input.workflow}",
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
        extra_metadata = {
            "temporalWorkflowID": info.workflow_id or "",
            "temporalRunID": info.workflow_run_id or "",
            "temporalActivityID": info.activity_id,
        }
        # Unconditionally set tracing context so @traceable functions inside
        # activities can use the plugin's LangSmith client and inherit parent.
        # When add_temporal_runs=True: maybe_run overrides with the RunActivity run.
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
            with self._config.maybe_run(
                f"RunActivity:{info.activity_type}",
                run_type="tool",
                parent=parent,
                extra_metadata=extra_metadata,
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
        self, name: str, headers: Mapping[str, Payload] | None = None
    ) -> Iterator[Any | None]:
        """Workflow-specific run creation with metadata.

        Extracts parent from headers (if provided) and stores the run (or parent
        fallback) as ``_current_run`` so the outbound interceptor can propagate
        context even when ``add_temporal_runs=False``.
        """
        parent = _extract_context(headers) if headers else None
        info = temporalio.workflow.info()
        extra_metadata = {
            "temporalWorkflowID": info.workflow_id,
            "temporalRunID": info.run_id,
        }
        with self._config.maybe_run(
            name, parent=parent, extra_metadata=extra_metadata
        ) as run:
            self._current_run = run or parent
            try:
                yield run
            finally:
                self._current_run = None

    async def execute_workflow(self, input: Any) -> Any:
        with self._workflow_maybe_run(
            f"RunWorkflow:{temporalio.workflow.info().workflow_type}", input.headers
        ):
            return await super().execute_workflow(input)

    async def handle_signal(self, input: Any) -> None:
        with self._workflow_maybe_run(f"HandleSignal:{input.signal}", input.headers):
            return await super().handle_signal(input)

    async def handle_query(self, input: Any) -> Any:
        with self._workflow_maybe_run(f"HandleQuery:{input.query}", input.headers):
            return await super().handle_query(input)

    def handle_update_validator(self, input: Any) -> None:
        with self._workflow_maybe_run(f"ValidateUpdate:{input.update}", input.headers):
            return super().handle_update_validator(input)

    async def handle_update_handler(self, input: Any) -> Any:
        with self._workflow_maybe_run(f"HandleUpdate:{input.update}", input.headers):
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
    def _traced_outbound(self, name: str, input: Any) -> Iterator[Any | None]:
        """Outbound workflow run creation with context injection into input.headers."""
        with self._config.maybe_run(name, parent=self._inbound._current_run) as run:
            context_source = run or self._inbound._current_run
            if context_source:
                input.headers = _inject_context(input.headers, context_source)
            yield run

    def start_activity(self, input: Any) -> Any:
        with self._traced_outbound(f"StartActivity:{input.activity}", input):
            return super().start_activity(input)

    def start_local_activity(self, input: Any) -> Any:
        with self._traced_outbound(f"StartActivity:{input.activity}", input):
            return super().start_local_activity(input)

    async def start_child_workflow(self, input: Any) -> Any:
        with self._traced_outbound(f"StartChildWorkflow:{input.workflow}", input):
            return await super().start_child_workflow(input)

    async def signal_child_workflow(self, input: Any) -> None:
        with self._traced_outbound(f"SignalChildWorkflow:{input.signal}", input):
            return await super().signal_child_workflow(input)

    async def signal_external_workflow(self, input: Any) -> None:
        with self._traced_outbound(f"SignalExternalWorkflow:{input.signal}", input):
            return await super().signal_external_workflow(input)

    def continue_as_new(self, input: Any) -> NoReturn:
        # No trace created, but inject context from inbound's current run
        current_run = getattr(self._inbound, "_current_run", None)
        if current_run:
            input.headers = _inject_context(input.headers, current_run)
        super().continue_as_new(input)

    async def start_nexus_operation(self, input: Any) -> Any:
        with self._config.maybe_run(
            f"StartNexusOperation:{input.service}/{input.operation_name}",
            parent=self._inbound._current_run,
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
        with self._config.maybe_run(
            f"RunStartNexusOperationHandler:{input.ctx.service}/{input.ctx.operation}",
            run_type="tool",
            parent=parent,
        ):
            return await self.next.execute_nexus_operation_start(input)

    async def execute_nexus_operation_cancel(self, input: Any) -> Any:
        parent = _extract_nexus_context(input.ctx.headers)
        with self._config.maybe_run(
            f"RunCancelNexusOperationHandler:{input.ctx.service}/{input.ctx.operation}",
            run_type="tool",
            parent=parent,
        ):
            return await self.next.execute_nexus_operation_cancel(input)
