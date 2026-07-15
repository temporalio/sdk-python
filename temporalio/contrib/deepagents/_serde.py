"""Serialization helpers, a result cache, and worker-runtime configuration.

The Deep Agents control loop runs *inside* the Temporal workflow, so the values
that actually cross the workflow⇄activity boundary are a small set of LangChain
types: chat messages, tool-call descriptors, and the ``RunnableConfig`` metadata
attached to each model / tool call. LangChain messages are polymorphic
``Serializable`` models (an ``AIMessage`` must not be rehydrated as a
``ToolMessage``) and ``RunnableConfig`` carries live callback / checkpointer
references, so neither survives a naive round-trip. This module owns:

* message (de)serialization via ``langchain_core.load.dumpd`` / ``load`` — the
  round-trip that preserves message subtype and tool-call structure;
* the strip → ship → rebuild dance for ``RunnableConfig``;
* tool → JSON-schema advertisement (full name + description + argument schema,
  never ``{name, description}`` alone — without the argument schema the model
  picks the right tool but hallucinates its arguments);
* the Pydantic data converter (``exclude_unset=True``) the plugin installs on
  the client and replayer, so message payloads stay small and round-trip
  cleanly;
* a workflow-scoped result cache so model / tool results computed before a
  ``continue_as_new`` are reused rather than recomputed after it;
* the sandbox passthrough module list covering LangChain's transitive
  eager-import tree.

LangChain imports are deferred into the functions that need them, so importing
this module — and constructing the plugin — does not require LangChain to be
installed on the machine assembling the worker.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from temporalio.contrib._langchain._converter import (
    LangChainPayloadConverter,
)
from temporalio.contrib._langchain._converter import (
    build_data_converter as _shared_build_data_converter,
)
from temporalio.contrib._langchain._converter import (
    data_converter as data_converter,
)
from temporalio.contrib._langchain._messages import (
    dump_messages as dump_messages,
)
from temporalio.contrib._langchain._messages import (
    dump_object as dump_object,
)
from temporalio.contrib._langchain._messages import (
    load_messages as load_messages,
)
from temporalio.contrib._langchain._messages import (
    load_object as load_object,
)
from temporalio.contrib._langchain._messages import (
    tool_to_schema as tool_to_schema,
)
from temporalio.contrib._langchain._passthrough import merge_passthrough_modules
from temporalio.contrib._langchain._runnable_config import is_jsonish
from temporalio.contrib._langchain._runnable_config import (
    rebuild_runnable_config as rebuild_runnable_config,
)
from temporalio.contrib._langchain._runnable_config import (
    strip_runnable_config as _shared_strip_runnable_config,
)
from temporalio.contrib._langchain._task_cache import TaskResultCache
from temporalio.contrib._langchain._task_cache import cache_key as _shared_cache_key
from temporalio.converter import DataConverter

# ---------------------------------------------------------------------------
# Worker-wide dispatch settings
# ---------------------------------------------------------------------------
#
# These are fixed for the worker's lifetime (not per-workflow state), so a module
# global is correct. This module lives under ``temporalio``, which the workflow
# sandbox passes through, so the object the in-workflow model stub reads is the
# same one the plugin configured. They live here (not in ``_model``) so that
# ``DeepAgentsPlugin`` can push them down without importing ``_model`` — which
# would drag in LangChain at plugin-construction time.


@dataclasses.dataclass
class Settings:
    """Dispatch defaults shared by every ``TemporalModel`` on the worker."""

    model_activity_options: Any = None
    """``ActivityConfig`` or ``Mapping[model_name, ActivityConfig]``."""
    streaming_topic: str | None = None


_settings = Settings()


def set_settings(
    *,
    model_activity_options: Any = None,
    streaming_topic: str | None = None,
) -> None:
    """Install the worker-wide model dispatch defaults (called by the plugin)."""
    _settings.model_activity_options = model_activity_options
    _settings.streaming_topic = streaming_topic


def get_settings() -> Settings:
    """Return the active dispatch settings."""
    return _settings


# ---------------------------------------------------------------------------
# Data converter
# ---------------------------------------------------------------------------


# The family converter, kept under this plugin's historical name.
DeepAgentsPayloadConverter = LangChainPayloadConverter


def build_data_converter(
    user_converter: DataConverter | None,
) -> DataConverter:
    """Compose the plugin's converter with whatever the caller already set.

    Delegates to the shared family implementation; see its docstring for the
    None / SDK-default / custom-converter contract.
    """
    return _shared_build_data_converter(user_converter, plugin_name="DeepAgentsPlugin")


# ---------------------------------------------------------------------------
# LangChain object (de)serialization
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Backend protocol result (de)serialization
# ---------------------------------------------------------------------------

_BACKEND_DATACLASS_KEY = "__deepagents_dataclass__"


def dump_backend_result(value: Any) -> Any:
    """Encode a backend op's return value for the activity boundary.

    Backend protocol results (``WriteResult`` / ``ReadResult`` / ``GrepResult``
    and their nested ``FileInfo`` / ``GrepMatch`` items, …) are plain
    dataclasses — not LangChain ``Serializable`` objects — and the filesystem
    middleware reads their ATTRIBUTES in-workflow, so a plain JSON round-trip
    (which decays them to dicts) breaks the seam at the first real backend op.
    Tag deepagents dataclasses with their import path so
    :func:`load_backend_result` rebuilds the real type; anything else (str,
    dict, a custom backend's own types) passes through with today's
    plain-JSON behavior.
    """
    import dataclasses

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        cls = type(value)
        dumped_fields = {
            f.name: dump_backend_result(getattr(value, f.name))
            for f in dataclasses.fields(value)
        }
        if cls.__module__.split(".", 1)[0] == "deepagents":
            return {
                _BACKEND_DATACLASS_KEY: f"{cls.__module__}:{cls.__qualname__}",
                "fields": dumped_fields,
            }
        return dumped_fields
    if isinstance(value, (list, tuple)):
        return [dump_backend_result(v) for v in value]
    if isinstance(value, dict):
        return {k: dump_backend_result(v) for k, v in value.items()}
    return value


def load_backend_result(value: Any) -> Any:
    """Rebuild a value produced by :func:`dump_backend_result`.

    Only ``deepagents.*`` dataclasses are reconstructed (the tag is written
    exclusively for them); anything else would mean a forged payload, so
    refuse rather than import arbitrary types. Reconstruction suppresses
    ``DeprecationWarning``: this is transport, not user code — the backend
    already constructed the object once on the activity side, and required
    deprecated fields (e.g. ``WriteResult.files_update``) would otherwise
    warn on every op. Field values equal to a declared default are omitted
    from the constructor call.
    """
    import dataclasses
    import importlib
    import warnings

    if isinstance(value, dict) and _BACKEND_DATACLASS_KEY in value:
        path = value[_BACKEND_DATACLASS_KEY]
        module_name, _, qualname = path.partition(":")
        if module_name.split(".", 1)[0] != "deepagents":
            raise ValueError(f"refusing to rebuild non-deepagents type {path!r}")
        obj: Any = importlib.import_module(module_name)
        for part in qualname.split("."):
            obj = getattr(obj, part)
        if not (isinstance(obj, type) and dataclasses.is_dataclass(obj)):
            raise ValueError(f"{path!r} is not a dataclass")
        loaded = {k: load_backend_result(v) for k, v in value["fields"].items()}
        kwargs = {}
        for f in dataclasses.fields(obj):
            if f.name not in loaded:
                continue
            if f.default is not dataclasses.MISSING and loaded[f.name] == f.default:
                continue
            kwargs[f.name] = loaded[f.name]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            return obj(**kwargs)
    if isinstance(value, list):
        return [load_backend_result(v) for v in value]
    if isinstance(value, dict):
        return {k: load_backend_result(v) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------
# RunnableConfig strip / rebuild
# ---------------------------------------------------------------------------


# Shared with the other LangChain-family plugins; kept under the old private
# name because sibling modules reference it.
_is_jsonish = is_jsonish


def _keep_configurable(key: str, value: Any) -> bool:
    """Keep non-dunder, JSON-safe configurable entries."""
    return not key.startswith("__") and is_jsonish(value)


def strip_runnable_config(config: Any) -> dict[str, Any]:
    """Reduce a live ``RunnableConfig`` to its JSON-safe subset for shipping.

    Keeps ``tags`` and ``metadata`` (always present, values filtered to
    JSON-safe ones), truthy ``run_name`` / ``run_id``, ``recursion_limit``,
    and the JSON-safe non-dunder ``configurable`` keys. Drops callbacks,
    checkpointer / store / cache handles and every other live reference —
    those are reconstructed activity-side. Output shape follows the shared
    (langgraph-vetted) implementation.
    """
    return _shared_strip_runnable_config(
        config,
        configurable_filter=_keep_configurable,
        metadata_filter=is_jsonish,
    )


# ---------------------------------------------------------------------------
# Result cache (continue-as-new dedup)
# ---------------------------------------------------------------------------

# Per-workflow state: set at the top of the workflow run, read by the model and
# tool dispatch paths, snapshotted for continue-as-new. Built on the shared
# ContextVar-backed cache (update / signal handler tasks spawned by the
# workflow inherit it automatically); this plugin's instance is distinct from
# the langgraph plugin's.
_result_cache = TaskResultCache("_deepagents_result_cache")


def set_result_cache(cache: dict[str, Any] | None) -> None:
    """Seed the workflow-scoped result cache (e.g. carried across CAN)."""
    _result_cache.set_cache(dict(cache) if cache else {})


def result_cache_snapshot() -> dict[str, Any] | None:
    """Return a serializable copy of the cache, or ``None`` when empty."""
    cache = _result_cache.get_cache()
    return dict(cache) if cache else None


def cache_key(kind: str, call_id: str, args: Any) -> str:
    """Stable key over ``(kind, call_id, args)`` for cache lookups."""
    return _shared_cache_key(kind, (call_id, args), {})


def cache_lookup(key: str) -> tuple[bool, Any]:
    """Return ``(hit, value)`` for ``key`` in the active cache."""
    return _result_cache.lookup(key)


def cache_put(key: str, value: Any) -> None:
    """Record ``value`` under ``key`` when a cache is active for this run."""
    _result_cache.put(key, value)


# ---------------------------------------------------------------------------
# Sandbox passthrough
# ---------------------------------------------------------------------------

# The workflow sandbox re-imports modules per run; LangChain / LangGraph /
# deepagents build large class hierarchies with eager import side effects, and
# LangSmith pulls in numpy. Passing them through means "import once in the host
# and share", which is both faster and required for identity checks
# (isinstance across the sandbox boundary) to hold.
_DEFAULT_PASSTHROUGH: tuple[str, ...] = (
    "langchain",
    "langchain_core",
    "langchain_anthropic",
    "langgraph",
    "deepagents",
    "langsmith",
    "numpy",
    "pydantic",
    "pydantic_core",
    "anthropic",
    "tiktoken",
    "jsonpatch",
    "jsonpointer",
    "tenacity",
    "orjson",
    "httpx",
    "httpcore",
)


def default_passthrough_modules() -> tuple[str, ...]:
    """The LangChain / deepagents transitive import tree passed through the sandbox."""
    return _DEFAULT_PASSTHROUGH


def resolve_passthrough_modules(user: Any) -> tuple[str, ...]:
    """Merge caller-supplied passthrough modules with the plugin defaults."""
    return merge_passthrough_modules(_DEFAULT_PASSTHROUGH, user)
