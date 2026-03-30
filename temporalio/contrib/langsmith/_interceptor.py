"""LangSmith interceptor implementation for Temporal SDK."""

from __future__ import annotations

import json
import logging
import random
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, ClassVar, NoReturn, Protocol

import langsmith
import nexusrpc.handler
from langsmith import tracing_context
from langsmith.run_helpers import get_current_run_tree
from langsmith.run_trees import RunTree, WriteReplica

import temporalio.activity
import temporalio.client
import temporalio.converter
import temporalio.worker
import temporalio.workflow
from temporalio.api.common.v1 import Payload
from temporalio.exceptions import ApplicationError, ApplicationErrorCategory

# This logger is only used in _log_future_exception, which runs on the
# executor thread (not the workflow thread).  Never log directly from
# workflow interceptor code — the sandbox blocks logging I/O.
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HEADER_KEY = "_temporal-langsmith-context"

_BUILTIN_QUERIES: frozenset[str] = frozenset(
    {
        "__stack_trace",
        "__enhanced_stack_trace",
    }
)

# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------

_payload_converter = temporalio.converter.PayloadConverter.default


class _InputWithHeaders(Protocol):
    headers: Mapping[str, Payload]


def _inject_context(
    headers: Mapping[str, Payload],
    run_tree: RunTree,
) -> dict[str, Payload]:
    """Inject LangSmith context into Temporal payload headers.

    Serializes the run's trace context (trace ID, parent run ID, dotted order)
    into a Temporal header under ``_temporal-langsmith-context``, enabling parent-child
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
    executor: ThreadPoolExecutor,
) -> _ReplaySafeRunTree | None:
    """Extract LangSmith context from Temporal payload headers.

    Reconstructs a ``RunTree`` from the ``_temporal-langsmith-context`` header on
    the receiving side, wrapped in a :class:`_ReplaySafeRunTree` so inbound
    interceptors can establish a parent-child relationship with the sender's
    run. Returns ``None`` if no header is present.
    """
    header = headers.get(HEADER_KEY)
    if not header:
        return None
    ls_headers = _payload_converter.from_payloads([header])[0]
    run = RunTree.from_headers(ls_headers)
    return _ReplaySafeRunTree(run, executor=executor) if run else None


def _inject_nexus_context(
    headers: Mapping[str, str],
    run_tree: RunTree,
) -> dict[str, str]:
    """Inject LangSmith context into Nexus string headers."""
    ls_headers = run_tree.to_headers()
    return {
        **headers,
        HEADER_KEY: json.dumps(ls_headers),
    }


def _extract_nexus_context(
    headers: Mapping[str, str],
    executor: ThreadPoolExecutor,
) -> _ReplaySafeRunTree | None:
    """Extract LangSmith context from Nexus string headers."""
    raw = headers.get(HEADER_KEY)
    if not raw:
        return None
    ls_headers = json.loads(raw)
    run = RunTree.from_headers(ls_headers)
    return _ReplaySafeRunTree(run, executor=executor) if run else None


def _get_current_run_for_propagation() -> RunTree | None:
    """Get the current ambient run for context propagation.

    Filters out _ContextBridgeRunTree, which is internal scaffolding
    that should never be serialized into headers or used as parent runs.
    """
    run = get_current_run_tree()
    if isinstance(run, _ContextBridgeRunTree):
        return None
    return run


# ---------------------------------------------------------------------------
# Sandbox safety: patch @traceable's aio_to_thread
# ---------------------------------------------------------------------------

_aio_to_thread_patched = False


def _patch_aio_to_thread() -> None:
    """Patch langsmith's ``aio_to_thread`` to run synchronously in workflows.

    The ``@traceable`` decorator on async functions uses ``aio_to_thread()`` →
    ``loop.run_in_executor()`` for run setup/teardown.  The Temporal workflow
    sandbox blocks ``run_in_executor``.  This patch runs those functions
    synchronously (they are CPU-bound, no I/O) when inside a workflow.

    """
    global _aio_to_thread_patched  # noqa: PLW0603
    if _aio_to_thread_patched:
        return

    import langsmith._internal._aiter as _aiter

    _original = _aiter.aio_to_thread

    import contextvars

    async def _safe_aio_to_thread(
        func: Callable[..., Any],
        /,
        *args: Any,
        __ctx: contextvars.Context | None = None,
        **kwargs: Any,
    ) -> Any:
        if not temporalio.workflow.in_workflow():
            return await _original(func, *args, __ctx=__ctx, **kwargs)
        with temporalio.workflow.unsafe.sandbox_unrestricted():
            # Run func directly in the current context (no ctx.run) so
            # that context var changes (e.g. _PARENT_RUN_TREE set by
            # @traceable's _setup_run) propagate to the caller.
            # This is safe because workflows are single-threaded.
            #
            # No replay-time tracing disable — _ReplaySafeRunTree.post()
            # and patch() are no-ops during replay, which handles I/O
            # suppression. _setup_run must run normally during replay to
            # maintain parent-child linkage across the replay boundary.
            return func(*args, **kwargs)

    _aiter.aio_to_thread = _safe_aio_to_thread  # type: ignore[assignment]
    _aio_to_thread_patched = True


# ---------------------------------------------------------------------------
# Replay safety
# ---------------------------------------------------------------------------


def _is_replaying() -> bool:
    """Check if we're currently replaying workflow history."""
    return (
        temporalio.workflow.in_workflow()
        and temporalio.workflow.unsafe.is_replaying_history_events()
    )


def _get_workflow_random() -> random.Random | None:
    """Get a deterministic random generator for the current workflow.

    Creates a workflow-safe random generator once via
    ``workflow.new_random()`` and stores it on the workflow instance so
    subsequent calls return the same generator.  The generator is seeded
    from the workflow's deterministic seed, so it produces identical UUIDs
    across replays and worker restarts.

    Returns ``None`` outside a workflow, in read-only (query) contexts, or
    when workflow APIs are mocked (unit tests).
    """
    try:
        if not temporalio.workflow.in_workflow():
            return None
        if temporalio.workflow.unsafe.is_read_only():
            return None
        inst = temporalio.workflow.instance()
        rng = getattr(inst, "__temporal_langsmith_random", None)
        if rng is None:
            rng = temporalio.workflow.new_random()
            setattr(inst, "__temporal_langsmith_random", rng)
        return rng
    except Exception:
        return None


def _uuid_from_random(rng: random.Random) -> uuid.UUID:
    """Generate a deterministic UUID4 from a workflow-bound random generator."""
    return uuid.UUID(int=rng.getrandbits(128), version=4)


# ---------------------------------------------------------------------------
# _ReplaySafeRunTree wrapper
# ---------------------------------------------------------------------------


class _ReplaySafeRunTree(RunTree):
    """Wrapper around a ``RunTree`` with replay-safe ``post``, ``end``, and ``patch``.

    Inherits from ``RunTree`` so ``isinstance`` checks pass, but does
    **not** call ``super().__init__()``—the wrapped ``_run`` is the real
    RunTree.  Attribute access is delegated via ``__getattr__``/``__setattr__``.

    During replay, ``post()``, ``end()``, and ``patch()`` become no-ops.
    In workflow context, ``post()`` and ``patch()`` submit to a single-worker
    ``ThreadPoolExecutor`` for FIFO ordering, avoiding blocking on the
    workflow task thread.
    """

    def __init__(  # pyright: ignore[reportMissingSuperCall]
        self,
        run_tree: RunTree,
        *,
        executor: ThreadPoolExecutor,
    ) -> None:
        """Wrap an existing RunTree with replay-safe overrides."""
        object.__setattr__(self, "_run", run_tree)
        object.__setattr__(self, "_executor", executor)

    def __getattr__(self, name: str) -> Any:
        """Delegate attribute access to the wrapped RunTree."""
        return getattr(self._run, name)

    def __setattr__(self, name: str, value: Any) -> None:
        """Delegate attribute setting to the wrapped RunTree."""
        setattr(self._run, name, value)

    def to_headers(self) -> dict[str, str]:
        """Delegate to the wrapped RunTree's to_headers."""
        return self._run.to_headers()

    def _inject_deterministic_ids(self, kwargs: dict[str, Any]) -> None:
        """Inject deterministic run_id and start_time in workflow context."""
        if temporalio.workflow.in_workflow():
            if kwargs.get("run_id") is None:
                rng = _get_workflow_random()
                if rng is not None:
                    kwargs["run_id"] = _uuid_from_random(rng)
            if kwargs.get("start_time") is None:
                kwargs["start_time"] = temporalio.workflow.now()

    def create_child(self, *args: Any, **kwargs: Any) -> _ReplaySafeRunTree:
        """Create a child run, returning another _ReplaySafeRunTree.

        In workflow context, injects deterministic ``run_id`` and ``start_time``
        unless they are passed in manually via ``kwargs``.
        """
        self._inject_deterministic_ids(kwargs)
        child_run = self._run.create_child(*args, **kwargs)
        return _ReplaySafeRunTree(child_run, executor=self._executor)

    def _submit_or_fallback(
        self, fn: Callable[..., object], *args: Any, **kwargs: Any
    ) -> None:
        """Submit work to executor, falling back to synchronous after shutdown."""

        def _log_future_exception(future: Future[None]) -> None:
            exc = future.exception()
            if exc is not None:
                logger.error("LangSmith background I/O error: %s", exc)

        try:
            future = self._executor.submit(fn, *args, **kwargs)
            future.add_done_callback(_log_future_exception)
        except RuntimeError:
            # Executor shut down — fall back to synchronous execution
            fn(*args, **kwargs)

    def post(self, exclude_child_runs: bool = True) -> None:
        """Post the run to LangSmith, skipping during replay."""
        if temporalio.workflow.in_workflow():
            if _is_replaying():
                return
            with temporalio.workflow.unsafe.sandbox_unrestricted():
                self._submit_or_fallback(
                    self._run.post, exclude_child_runs=exclude_child_runs
                )
        else:
            self._run.post(exclude_child_runs=exclude_child_runs)

    def end(self, **kwargs: Any) -> None:
        """End the run, skipping during replay.

        No I/O — just sets attributes on self._run. Runs synchronously.
        """
        if _is_replaying():
            return
        if temporalio.workflow.in_workflow():
            with temporalio.workflow.unsafe.sandbox_unrestricted():
                self._run.end(**kwargs)
        else:
            self._run.end(**kwargs)

    def patch(self, *, exclude_inputs: bool = False) -> None:
        """Patch the run to LangSmith, skipping during replay."""
        if temporalio.workflow.in_workflow():
            if _is_replaying():
                return
            with temporalio.workflow.unsafe.sandbox_unrestricted():
                self._submit_or_fallback(self._run.patch, exclude_inputs=exclude_inputs)
        else:
            self._run.patch(exclude_inputs=exclude_inputs)


class _ContextBridgeRunTree(_ReplaySafeRunTree):
    """Lightweight bridge for ``add_temporal_runs=False`` without external context.

    Never posted, patched, or ended — no trace of it exists in LangSmith.
    ``create_child()`` creates root ``_ReplaySafeRunTree`` objects (no
    ``parent_run_id``) so that ``@traceable`` calls appear as independent
    root runs.
    """

    def __init__(  # pyright: ignore[reportMissingSuperCall]
        self,
        *,
        ls_client: langsmith.Client,
        executor: ThreadPoolExecutor,
        session_name: str | None = None,
        replicas: Sequence[WriteReplica] | None = None,
    ) -> None:
        """Create a context bridge with the given LangSmith client."""
        # Create a minimal RunTree for the bridge — it will never be posted
        bridge_run = RunTree(
            name="__bridge__",
            run_type="chain",
            ls_client=ls_client,
        )
        if session_name is not None:
            bridge_run.session_name = session_name
        if replicas is not None:
            bridge_run.replicas = replicas
        object.__setattr__(self, "_run", bridge_run)
        object.__setattr__(self, "_executor", executor)

    def post(self, exclude_child_runs: bool = True) -> NoReturn:
        """Bridge must never be posted."""
        raise RuntimeError("ContextBridgeRunTree must never be posted")

    def patch(self, *, exclude_inputs: bool = False) -> NoReturn:
        """Bridge must never be patched."""
        raise RuntimeError("ContextBridgeRunTree must never be patched")

    def end(self, **kwargs: Any) -> NoReturn:
        """Bridge must never be ended."""
        raise RuntimeError("ContextBridgeRunTree must never be ended")

    def create_child(self, *args: Any, **kwargs: Any) -> _ReplaySafeRunTree:
        """Create a root _ReplaySafeRunTree (no parent_run_id).

        Creates a fresh ``RunTree(...)`` directly (not via
        ``self._run.create_child``) to avoid setting ``parent_run_id``,
        ``parent_dotted_order``, and ``trace_id`` to the bridge's values.
        Maps ``run_id`` → ``id`` matching LangSmith's ``create_child`` convention.
        """
        self._inject_deterministic_ids(kwargs)

        # Map run_id → id (matching RunTree.create_child convention)
        if "run_id" in kwargs:
            kwargs["id"] = kwargs.pop("run_id")

        # Inherit ls_client and session_name from bridge via constructor.
        # session_name is a Pydantic field (alias="project_name") so it
        # must be passed at construction to avoid the "default" fallback.
        kwargs.setdefault("ls_client", self._run.ls_client)
        kwargs.setdefault("session_name", self._run.session_name)

        child_run = RunTree(*args, **kwargs)
        # Set replicas post-construction (not a RunTree Pydantic field)
        if self._run.replicas is not None:
            child_run.replicas = self._run.replicas
        return _ReplaySafeRunTree(child_run, executor=self._executor)


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
    client: langsmith.Client,
    name: str,
    *,
    add_temporal_runs: bool,
    run_type: str = "chain",
    inputs: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    parent: RunTree | None = None,
    project_name: str | None = None,
    executor: ThreadPoolExecutor,
) -> Iterator[_ReplaySafeRunTree | None]:
    """Create a LangSmith run, handling errors.

    - If add_temporal_runs is False, yields None (no run created).
      Context propagation is handled unconditionally by callers.
    - When a run IS created, uses :class:`_ReplaySafeRunTree` for
      replay and sandbox safety, then sets it as ambient context via
      ``tracing_context(parent=run_tree)`` so ``get_current_run_tree()``
      returns it and ``_inject_current_context()`` can inject it.
    - On exception: marks run as errored (unless benign ApplicationError), re-raises.

    Args:
        client: LangSmith client instance.
        name: Display name for the run.
        add_temporal_runs: Whether to create Temporal-level trace runs.
        run_type: LangSmith run type (default ``"chain"``).
        inputs: Input data to record on the run.
        metadata: Extra metadata to attach to the run.
        tags: Tags to attach to the run.
        parent: Parent run for nesting.
        project_name: LangSmith project name override.
        executor: ThreadPoolExecutor for background I/O.
    """
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
    # Deterministic IDs and start times in workflow context so that
    # replayed workflows produce identical LangSmith runs instead of
    # duplicates.  In production, a workflow can be evicted from the
    # worker cache and later replayed on a different worker — without
    # deterministic IDs the replayed execution would create a second
    # run for the same logical operation.  Uses a workflow-bound random
    # generator seeded from the workflow's deterministic seed, so UUIDs
    # are identical across replays.
    rng = _get_workflow_random()
    if rng is not None:
        kwargs["id"] = _uuid_from_random(rng)
        kwargs["start_time"] = temporalio.workflow.now()
    elif temporalio.workflow.in_workflow():
        # Read-only context (e.g. query handler) — use workflow.uuid4()
        try:
            kwargs["id"] = temporalio.workflow.uuid4()
            kwargs["start_time"] = temporalio.workflow.now()
        except Exception:
            pass  # Not in a real workflow context (e.g., unit test mock)
    if project_name is not None:
        kwargs["project_name"] = project_name
    if parent is not None:
        # Unwrap _ReplaySafeRunTree so RunTree gets the real parent
        kwargs["parent_run"] = (
            parent._run if isinstance(parent, _ReplaySafeRunTree) else parent
        )
    if metadata:
        kwargs["extra"] = {"metadata": metadata}
    if tags:
        kwargs["tags"] = tags
    run_tree = _ReplaySafeRunTree(RunTree(**kwargs), executor=executor)
    run_tree.post()
    try:
        with tracing_context(parent=run_tree, client=client):
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
        client: langsmith.Client | None = None,
        project_name: str | None = None,
        add_temporal_runs: bool = False,
        default_metadata: dict[str, Any] | None = None,
        default_tags: list[str] | None = None,
    ) -> None:
        """Initialize the LangSmith interceptor with tracing configuration."""
        super().__init__()
        # Import langsmith.Client lazily to avoid hard dependency at import time
        if client is None:
            import langsmith

            client = langsmith.Client()
        self._client = client
        self._project_name = project_name
        self._add_temporal_runs = add_temporal_runs
        self._default_metadata = default_metadata or {}
        self._default_tags = default_tags or []
        self._executor = ThreadPoolExecutor(max_workers=1)

    @contextmanager
    def maybe_run(
        self,
        name: str,
        *,
        run_type: str = "chain",
        parent: RunTree | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> Iterator[_ReplaySafeRunTree | None]:
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
            executor=self._executor,
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
        _patch_aio_to_thread()
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
    def _traced_call(self, name: str, input: _InputWithHeaders) -> Iterator[None]:
        """Wrap a client call with a LangSmith run and inject context into headers."""
        with self._config.maybe_run(name):
            input.headers = _inject_current_context(input.headers)
            yield

    async def start_workflow(
        self, input: temporalio.client.StartWorkflowInput
    ) -> temporalio.client.WorkflowHandle[Any, Any]:
        prefix = "SignalWithStartWorkflow" if input.start_signal else "StartWorkflow"
        with self._traced_call(f"{prefix}:{input.workflow}", input):
            return await super().start_workflow(input)

    async def query_workflow(self, input: temporalio.client.QueryWorkflowInput) -> Any:
        with self._traced_call(f"QueryWorkflow:{input.query}", input):
            return await super().query_workflow(input)

    async def signal_workflow(
        self, input: temporalio.client.SignalWorkflowInput
    ) -> None:
        with self._traced_call(f"SignalWorkflow:{input.signal}", input):
            return await super().signal_workflow(input)

    async def start_workflow_update(
        self, input: temporalio.client.StartWorkflowUpdateInput
    ) -> temporalio.client.WorkflowUpdateHandle[Any]:
        with self._traced_call(f"StartWorkflowUpdate:{input.update}", input):
            return await super().start_workflow_update(input)

    async def start_update_with_start_workflow(
        self, input: temporalio.client.StartWorkflowUpdateWithStartInput
    ) -> temporalio.client.WorkflowUpdateHandle[Any]:
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

    async def execute_activity(
        self, input: temporalio.worker.ExecuteActivityInput
    ) -> Any:
        parent = _extract_context(input.headers, self._config._executor)
        info = temporalio.activity.info()
        extra_metadata = {
            "temporalWorkflowID": info.workflow_id or "",
            "temporalRunID": info.workflow_run_id or "",
            "temporalActivityID": info.activity_id or "",
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
        if self._config._project_name:
            ctx_kwargs["project_name"] = self._config._project_name
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

    _config: ClassVar[LangSmithInterceptor]

    def init(self, outbound: temporalio.worker.WorkflowOutboundInterceptor) -> None:
        super().init(_LangSmithWorkflowOutboundInterceptor(outbound, self._config))

    @contextmanager
    def _workflow_maybe_run(
        self,
        name: str,
        headers: Mapping[str, Payload] | None = None,
    ) -> Iterator[_ReplaySafeRunTree | None]:
        """Workflow-specific run creation with metadata.

        Extracts parent from headers (if provided) and sets up
        ``tracing_context`` so ``@traceable`` functions called from workflow
        code can discover the parent and LangSmith client, independent of the
        ``add_temporal_runs`` toggle.
        """
        parent = _extract_context(headers, self._config._executor) if headers else None
        if parent is not None:
            parent.ls_client = self._config._client
        info = temporalio.workflow.info()
        extra_metadata = {
            "temporalWorkflowID": info.workflow_id,
            "temporalRunID": info.run_id,
        }
        # Set up tracing context for @traceable functions inside the workflow.
        # When add_temporal_runs=True, _maybe_run overrides with the
        # RunWorkflow run as parent.  When False, this outer context ensures
        # @traceable still sees the propagated parent from headers.
        ctx_kwargs: dict[str, Any] = {
            "client": self._config._client,
            "enabled": True,
        }
        # When add_temporal_runs=False and no external parent, create a
        # _ContextBridgeRunTree so @traceable calls get a _ReplaySafeRunTree
        # parent via create_child. The bridge is invisible in LangSmith.
        bridge: _ContextBridgeRunTree | None = None
        if not self._config._add_temporal_runs and parent is None:
            bridge = _ContextBridgeRunTree(
                ls_client=self._config._client,
                executor=self._config._executor,
                session_name=self._config._project_name,
            )
            ctx_kwargs["parent"] = bridge
            ctx_kwargs["project_name"] = self._config._project_name
        elif parent:
            ctx_kwargs["parent"] = parent
        with tracing_context(**ctx_kwargs):
            with self._config.maybe_run(
                name,
                parent=parent,
                extra_metadata=extra_metadata,
            ) as run:
                yield run

    async def execute_workflow(
        self, input: temporalio.worker.ExecuteWorkflowInput
    ) -> Any:
        wf_type = temporalio.workflow.info().workflow_type
        with self._workflow_maybe_run(
            f"RunWorkflow:{wf_type}",
            input.headers,
        ):
            return await super().execute_workflow(input)

    async def handle_signal(self, input: temporalio.worker.HandleSignalInput) -> None:
        with self._workflow_maybe_run(f"HandleSignal:{input.signal}", input.headers):
            return await super().handle_signal(input)

    async def handle_query(self, input: temporalio.worker.HandleQueryInput) -> Any:
        if input.query.startswith("__temporal") or input.query in _BUILTIN_QUERIES:
            return await super().handle_query(input)
        with self._workflow_maybe_run(f"HandleQuery:{input.query}", input.headers):
            return await super().handle_query(input)

    def handle_update_validator(
        self, input: temporalio.worker.HandleUpdateInput
    ) -> None:
        with self._workflow_maybe_run(f"ValidateUpdate:{input.update}", input.headers):
            return super().handle_update_validator(input)

    async def handle_update_handler(
        self, input: temporalio.worker.HandleUpdateInput
    ) -> Any:
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
    ) -> None:
        super().__init__(next)
        self._config = config

    @contextmanager
    def _traced_outbound(
        self, name: str, input: _InputWithHeaders
    ) -> Iterator[_ReplaySafeRunTree | None]:
        """Outbound workflow run creation with context injection into input.headers.

        Uses ambient context (``get_current_run_tree()``) instead of a cached
        snapshot, so ``@traceable`` step functions that wrap outbound calls
        correctly parent the outbound run under themselves.
        """
        with self._config.maybe_run(name) as run:
            context_source = run or _get_current_run_for_propagation()
            if context_source:
                input.headers = _inject_context(input.headers, context_source)
            yield run

    def start_activity(
        self, input: temporalio.worker.StartActivityInput
    ) -> temporalio.workflow.ActivityHandle[Any]:
        with self._traced_outbound(f"StartActivity:{input.activity}", input):
            return super().start_activity(input)

    def start_local_activity(
        self, input: temporalio.worker.StartLocalActivityInput
    ) -> temporalio.workflow.ActivityHandle[Any]:
        with self._traced_outbound(f"StartActivity:{input.activity}", input):
            return super().start_local_activity(input)

    async def start_child_workflow(
        self, input: temporalio.worker.StartChildWorkflowInput
    ) -> temporalio.workflow.ChildWorkflowHandle[Any, Any]:
        with self._traced_outbound(f"StartChildWorkflow:{input.workflow}", input):
            return await super().start_child_workflow(input)

    async def signal_child_workflow(
        self, input: temporalio.worker.SignalChildWorkflowInput
    ) -> None:
        with self._traced_outbound(f"SignalChildWorkflow:{input.signal}", input):
            return await super().signal_child_workflow(input)

    async def signal_external_workflow(
        self, input: temporalio.worker.SignalExternalWorkflowInput
    ) -> None:
        with self._traced_outbound(f"SignalExternalWorkflow:{input.signal}", input):
            return await super().signal_external_workflow(input)

    def continue_as_new(self, input: temporalio.worker.ContinueAsNewInput) -> NoReturn:
        # No trace created, but inject context from ambient run
        current_run = _get_current_run_for_propagation()
        if current_run:
            input.headers = _inject_context(input.headers, current_run)
        super().continue_as_new(input)

    async def start_nexus_operation(
        self, input: temporalio.worker.StartNexusOperationInput[Any, Any]
    ) -> temporalio.workflow.NexusOperationHandle[Any]:
        with self._config.maybe_run(
            f"StartNexusOperation:{input.service}/{input.operation_name}",
        ) as run:
            context_source = run or _get_current_run_for_propagation()
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

    async def execute_nexus_operation_start(
        self, input: temporalio.worker.ExecuteNexusOperationStartInput
    ) -> (
        nexusrpc.handler.StartOperationResultSync[Any]
        | nexusrpc.handler.StartOperationResultAsync
    ):
        parent = _extract_nexus_context(input.ctx.headers, self._config._executor)
        if parent is not None and hasattr(parent, "ls_client"):
            parent.ls_client = self._config._client
        ctx_kwargs: dict[str, Any] = {
            "client": self._config._client,
            "enabled": True,
        }
        if self._config._project_name:
            ctx_kwargs["project_name"] = self._config._project_name
        if parent:
            ctx_kwargs["parent"] = parent
        with tracing_context(**ctx_kwargs):
            with self._config.maybe_run(
                f"RunStartNexusOperationHandler:{input.ctx.service}/{input.ctx.operation}",
                run_type="tool",
                parent=parent,
            ):
                return await self.next.execute_nexus_operation_start(input)

    async def execute_nexus_operation_cancel(
        self, input: temporalio.worker.ExecuteNexusOperationCancelInput
    ) -> None:
        parent = _extract_nexus_context(input.ctx.headers, self._config._executor)
        if parent is not None and hasattr(parent, "ls_client"):
            parent.ls_client = self._config._client
        ctx_kwargs: dict[str, Any] = {
            "client": self._config._client,
            "enabled": True,
        }
        if self._config._project_name:
            ctx_kwargs["project_name"] = self._config._project_name
        if parent:
            ctx_kwargs["parent"] = parent
        with tracing_context(**ctx_kwargs):
            with self._config.maybe_run(
                f"RunCancelNexusOperationHandler:{input.ctx.service}/{input.ctx.operation}",
                run_type="tool",
                parent=parent,
            ):
                return await self.next.execute_nexus_operation_cancel(input)
