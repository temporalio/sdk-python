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

    Reads ``_get_current_run_for_propagation()`` and injects if present. Returns
    headers unchanged if no context is active. Called unconditionally so that
    context propagation is independent of the ``add_temporal_runs`` toggle.
    """
    current = _get_current_run_for_propagation()
    if current is not None:
        return _inject_context(headers, current)
    return headers


def _extract_context(
    headers: Mapping[str, Payload],
    executor: ThreadPoolExecutor,
    ls_client: langsmith.Client,
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
    if run is None:
        return None
    run.ls_client = ls_client
    return _ReplaySafeRunTree(run, executor=executor)


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
    ls_client: langsmith.Client,
) -> _ReplaySafeRunTree | None:
    """Extract LangSmith context from Nexus string headers."""
    raw = headers.get(HEADER_KEY)
    if not raw:
        return None
    ls_headers = json.loads(raw)
    run = RunTree.from_headers(ls_headers)
    if run is None:
        return None
    run.ls_client = ls_client
    return _ReplaySafeRunTree(run, executor=executor)


def _get_current_run_for_propagation() -> RunTree | None:
    """Get the current ambient run for context propagation.

    Filters out ``_RootReplaySafeRunTreeFactory``, which is internal
    scaffolding that should never be serialized into headers or used as
    parent runs.
    """
    run = get_current_run_tree()
    if isinstance(run, _RootReplaySafeRunTreeFactory):
        return None
    return run


# ---------------------------------------------------------------------------
# Workflow event loop safety: patch @traceable's aio_to_thread
# ---------------------------------------------------------------------------

_aio_to_thread_patched = False


def _patch_aio_to_thread() -> None:
    """Patch langsmith's ``aio_to_thread`` to run synchronously in workflows.

    The ``@traceable`` decorator on async functions uses ``aio_to_thread()`` →
    ``loop.run_in_executor()`` for run setup/teardown.  The Temporal workflow
    event loop does not support ``run_in_executor``.  This patch runs those
    functions synchronously on the workflow thread when inside a workflow.
    Functions passed here must not perform blocking I/O.

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
            # Run without ctx.run() so context var changes propagate
            # to the caller. Safe because workflows are single-threaded.
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

    During replay, ``post()``, ``end()``, and ``patch()`` become no-ops
    (I/O suppression), but ``create_child()`` still runs to maintain
    parent-child linkage so ``@traceable``'s ``_setup_run`` can build the
    run tree across the replay boundary.  In workflow context, ``post()``
    and ``patch()`` submit to a single-worker ``ThreadPoolExecutor`` for
    FIFO ordering, avoiding blocking on the workflow task thread.
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

    def _submit(self, fn: Callable[..., object], *args: Any, **kwargs: Any) -> None:
        """Submit work to the background executor."""

        def _log_future_exception(future: Future[None]) -> None:
            exc = future.exception()
            if exc is not None:
                logger.error("LangSmith background I/O error: %s", exc)

        future = self._executor.submit(fn, *args, **kwargs)
        future.add_done_callback(_log_future_exception)

    def post(self, exclude_child_runs: bool = True) -> None:
        """Post the run to LangSmith, skipping during replay."""
        if temporalio.workflow.in_workflow():
            if _is_replaying():
                return
            self._submit(self._run.post, exclude_child_runs=exclude_child_runs)
        else:
            self._run.post(exclude_child_runs=exclude_child_runs)

    def end(self, **kwargs: Any) -> None:
        """End the run, skipping during replay.

        Pre-computes ``end_time`` via ``workflow.now()`` in workflow context
        so ``RunTree.end()`` doesn't call ``datetime.now()`` (non-deterministic
        and sandbox-restricted).
        """
        if _is_replaying():
            return
        if temporalio.workflow.in_workflow():
            kwargs.setdefault("end_time", temporalio.workflow.now())
        self._run.end(**kwargs)

    def patch(self, *, exclude_inputs: bool = False) -> None:
        """Patch the run to LangSmith, skipping during replay."""
        if temporalio.workflow.in_workflow():
            if _is_replaying():
                return
            self._submit(self._run.patch, exclude_inputs=exclude_inputs)
        else:
            self._run.patch(exclude_inputs=exclude_inputs)


class _RootReplaySafeRunTreeFactory(_ReplaySafeRunTree):
    """Factory that produces independent root ``_ReplaySafeRunTree`` instances with no parent link.

    When ``add_temporal_runs=False`` and no parent was propagated via headers,
    ``@traceable`` functions still need *something* in the LangSmith
    ``tracing_context`` to call ``create_child()`` on — otherwise they
    cannot create ``_ReplaySafeRunTree`` children at all and instead default to
    creating generic ``RunTree``s, which are not replay safe. This class fills
    that role: it sits in the context as the nominal parent so
    ``@traceable`` has a ``create_child()`` target.

    However, ``create_child()`` deliberately creates fresh ``RunTree``
    instances with **no** ``parent_run_id``.  This means every child appears
    as an independent root run in LangSmith rather than being nested under
    a phantom parent that was never meant to be visible.

    ``post()``, ``patch()``, and ``end()`` all raise ``RuntimeError``
    because this object is purely internal scaffolding — it must never
    appear in LangSmith.  If any of these methods are called, it indicates
    a programming error.
    """

    def __init__(  # pyright: ignore[reportMissingSuperCall]
        self,
        *,
        ls_client: langsmith.Client,
        executor: ThreadPoolExecutor,
        session_name: str | None = None,
        replicas: Sequence[WriteReplica] | None = None,
    ) -> None:
        """Create a root factory with the given LangSmith client."""
        # Create a minimal RunTree for the factory — it will never be posted
        factory_run = RunTree(
            name="__root_factory__",
            run_type="chain",
            ls_client=ls_client,
        )
        if session_name is not None:
            factory_run.session_name = session_name
        if replicas is not None:
            factory_run.replicas = replicas
        object.__setattr__(self, "_run", factory_run)
        object.__setattr__(self, "_executor", executor)

    def post(self, exclude_child_runs: bool = True) -> NoReturn:
        """Factory must never be posted."""
        raise RuntimeError("_RootReplaySafeRunTreeFactory must never be posted")

    def patch(self, *, exclude_inputs: bool = False) -> NoReturn:
        """Factory must never be patched."""
        raise RuntimeError("_RootReplaySafeRunTreeFactory must never be patched")

    def end(self, **kwargs: Any) -> NoReturn:
        """Factory must never be ended."""
        raise RuntimeError("_RootReplaySafeRunTreeFactory must never be ended")

    def create_child(self, *args: Any, **kwargs: Any) -> _ReplaySafeRunTree:
        """Create a root _ReplaySafeRunTree (no parent_run_id).

        Creates a fresh ``RunTree(...)`` directly (bypassing
        ``self._run.create_child``) so children are independent root runs
        with no link back to the factory.
        """
        self._inject_deterministic_ids(kwargs)

        # RunTree expects "id", but callers pass "run_id". RunTree.create_child
        # also does the same mapping internally.
        if "run_id" in kwargs:
            kwargs["id"] = kwargs.pop("run_id")

        # Inherit ls_client and session_name from factory.
        # session_name must be passed at construction time.
        kwargs.setdefault("ls_client", self._run.ls_client)
        kwargs.setdefault("session_name", self._run.session_name)

        child_run = RunTree(*args, **kwargs)
        # Replicas must be set post-construction
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
) -> Iterator[None]:
    """Create a LangSmith run, handling errors.

    - If add_temporal_runs is False, yields None (no run created).
      Context propagation is handled unconditionally by callers.
    - When a run IS created, uses :class:`_ReplaySafeRunTree` for
      replay and event loop safety, then sets it as ambient context via
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
        parent = _get_current_run_for_propagation()

    run_tree_args: dict[str, Any] = dict(
        name=name,
        run_type=run_type,
        inputs=inputs or {},
        ls_client=client,
    )
    # Deterministic IDs so replayed workflows produce identical runs
    # instead of duplicates (see _get_workflow_random for details).
    rng = _get_workflow_random()
    # In read-only contexts (queries, update validators), _get_workflow_random()
    # returns None. Deterministic IDs aren't needed — these aren't replayed.
    # LangSmith will auto-generate a random UUID.
    if rng is not None:
        run_tree_args["id"] = _uuid_from_random(rng)
        run_tree_args["start_time"] = temporalio.workflow.now()
    if project_name is not None:
        run_tree_args["project_name"] = project_name
    if parent is not None:
        run_tree_args["parent_run"] = parent
    if metadata:
        run_tree_args["extra"] = {"metadata": metadata}
    if tags:
        run_tree_args["tags"] = tags
    run_tree = _ReplaySafeRunTree(RunTree(**run_tree_args), executor=executor)
    run_tree.post()
    try:
        with tracing_context(parent=run_tree, client=client):
            yield None
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

    .. warning::
        This class is experimental and may change in future versions.
        Use with caution in production environments.
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
        if client is None:
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
    ) -> Iterator[None]:
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

    @contextmanager
    def _traced_start(self, name: str, input: _InputWithHeaders) -> Iterator[None]:
        """Wrap a start operation, injecting ambient parent context before creating the run.

        Unlike ``_traced_call``, this injects headers *before* ``maybe_run``
        so the downstream ``RunFoo`` becomes a sibling of ``StartFoo`` rather
        than a child.
        """
        input.headers = _inject_current_context(input.headers)
        with self._config.maybe_run(name):
            yield

    async def start_workflow(
        self, input: temporalio.client.StartWorkflowInput
    ) -> temporalio.client.WorkflowHandle[Any, Any]:
        prefix = "SignalWithStartWorkflow" if input.start_signal else "StartWorkflow"
        with self._traced_start(f"{prefix}:{input.workflow}", input):
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
        input.start_workflow_input.headers = _inject_current_context(
            input.start_workflow_input.headers
        )
        input.update_workflow_input.headers = _inject_current_context(
            input.update_workflow_input.headers
        )
        with self._config.maybe_run(
            f"StartUpdateWithStartWorkflow:{input.start_workflow_input.workflow}",
        ):
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
        parent = _extract_context(
            input.headers, self._config._executor, self._config._client
        )
        info = temporalio.activity.info()
        extra_metadata = {
            "temporalWorkflowID": info.workflow_id or "",
            "temporalRunID": info.workflow_run_id or "",
            "temporalActivityID": info.activity_id or "",
        }
        # Unconditionally set tracing context so @traceable functions inside
        # activities inherit the plugin's client and parent, regardless of
        # the add_temporal_runs toggle.
        tracing_args: dict[str, Any] = {
            "client": self._config._client,
            "enabled": True,
            "project_name": self._config._project_name,
            "parent": parent,
        }
        with tracing_context(**tracing_args):
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
    ) -> Iterator[None]:
        """Workflow-specific run creation with metadata.

        Extracts parent from headers (if provided) and sets up
        ``tracing_context`` so ``@traceable`` functions called from workflow
        code can discover the parent and LangSmith client, independent of the
        ``add_temporal_runs`` toggle.
        """
        parent = (
            _extract_context(headers, self._config._executor, self._config._client)
            if headers
            else None
        )
        # When add_temporal_runs=False and no external parent, create a
        # _RootReplaySafeRunTreeFactory so @traceable calls get a
        # _ReplaySafeRunTree parent via create_child. The factory is
        # invisible in LangSmith.
        # tracing_parent can be None when add_temporal_runs=True but no parent was
        # propagated via headers — maybe_run will later create a root run in that case.
        tracing_parent: _ReplaySafeRunTree | _RootReplaySafeRunTreeFactory | None = (
            parent
            if parent is not None or self._config._add_temporal_runs
            else _RootReplaySafeRunTreeFactory(
                ls_client=self._config._client,
                executor=self._config._executor,
                session_name=self._config._project_name,
            )
        )
        tracing_args: dict[str, Any] = {
            "client": self._config._client,
            "enabled": True,
            "project_name": self._config._project_name,
            "parent": tracing_parent,
        }
        info = temporalio.workflow.info()
        extra_metadata = {
            "temporalWorkflowID": info.workflow_id,
            "temporalRunID": info.run_id,
        }
        with tracing_context(**tracing_args):
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
    def _traced_outbound(self, name: str, input: _InputWithHeaders) -> Iterator[None]:
        """Outbound workflow run creation with context injection into input.headers.

        Uses ambient context so ``@traceable`` step functions that wrap
        outbound calls correctly parent the outbound run under themselves.
        """
        context_source = _get_current_run_for_propagation()
        with self._config.maybe_run(name):
            if context_source:
                input.headers = _inject_context(input.headers, context_source)
            yield None

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
        context_source = _get_current_run_for_propagation()
        with self._config.maybe_run(
            f"StartNexusOperation:{input.service}/{input.operation_name}",
        ):
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
        parent = _extract_nexus_context(
            input.ctx.headers, self._config._executor, self._config._client
        )
        tracing_args: dict[str, Any] = {
            "client": self._config._client,
            "enabled": True,
            "project_name": self._config._project_name,
            "parent": parent,
        }
        with tracing_context(**tracing_args):
            with self._config.maybe_run(
                f"RunStartNexusOperationHandler:{input.ctx.service}/{input.ctx.operation}",
                run_type="tool",
                parent=parent,
            ):
                return await self.next.execute_nexus_operation_start(input)

    async def execute_nexus_operation_cancel(
        self, input: temporalio.worker.ExecuteNexusOperationCancelInput
    ) -> None:
        parent = _extract_nexus_context(
            input.ctx.headers, self._config._executor, self._config._client
        )
        tracing_args: dict[str, Any] = {
            "client": self._config._client,
            "enabled": True,
            "project_name": self._config._project_name,
            "parent": parent,
        }
        with tracing_context(**tracing_args):
            with self._config.maybe_run(
                f"RunCancelNexusOperationHandler:{input.ctx.service}/{input.ctx.operation}",
                run_type="tool",
                parent=parent,
            ):
                return await self.next.execute_nexus_operation_cancel(input)
