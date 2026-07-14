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

import contextvars
import dataclasses
import hashlib
import json
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

from temporalio.contrib.pydantic import PydanticPayloadConverter, ToJsonOptions
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


class DeepAgentsPayloadConverter(PydanticPayloadConverter):
    """Pydantic payload converter pinned to ``exclude_unset=True``.

    LangChain request/response types (and ``DeepAgentState``) are deeply nested
    with many ``Optional[...] = None`` fields. Shipping every unset default
    inflates payloads several-fold and some peers reject the explicit nulls on
    round-trip, so we exclude unset fields by convention.
    """

    def __init__(self) -> None:
        """Construct the converter with ``exclude_unset`` serialization."""
        super().__init__(ToJsonOptions(exclude_unset=True))


data_converter = DataConverter(payload_converter_class=DeepAgentsPayloadConverter)
"""The plugin's default data converter (LangChain messages are shipped as their
``dumpd`` JSON form, so the Pydantic converter only ever sees plain containers)."""


def build_data_converter(
    user_converter: DataConverter | None,
) -> DataConverter:
    """Compose the plugin's converter with whatever the caller already set.

    * ``None`` — install the plugin default.
    * the SDK default converter — swap in the LangChain-aware Pydantic
      converter via :func:`dataclasses.replace`.
    * a custom converter — refuse rather than silently clobber it; the caller
      must fold :class:`DeepAgentsPayloadConverter` into their own converter.
    """
    if user_converter is None:
        return data_converter
    if user_converter is DataConverter.default:
        return dataclasses.replace(
            user_converter, payload_converter_class=DeepAgentsPayloadConverter
        )
    raise ValueError(
        "DeepAgentsPlugin cannot compose with a custom data_converter "
        "automatically. Set payload_converter_class=DeepAgentsPayloadConverter "
        "on your own DataConverter (so LangChain messages serialize with "
        "exclude_unset=True), or omit data_converter to use the plugin default."
    )


# ---------------------------------------------------------------------------
# LangChain object (de)serialization
# ---------------------------------------------------------------------------


def dump_object(obj: Any) -> Any:
    """Serialize a single LangChain ``Serializable`` (message, tool call, …)."""
    from langchain_core.load import dumpd

    return dumpd(obj)


def load_object(data: Any) -> Any:
    """Rehydrate a value produced by :func:`dump_object`, preserving subtype."""
    from langchain_core.load import load

    return load(data)


# ---------------------------------------------------------------------------
# Backend protocol result (de)serialization
# ---------------------------------------------------------------------------

_BACKEND_DATACLASS_KEY = "__deepagents_dataclass__"


def dump_backend_result(value: Any) -> Any:
    """Encode a backend op's return value for the activity boundary.

    Backend protocol results (``WriteResult`` / ``ReadResult`` / ``GrepResult``
    and their nested ``FileInfo`` / ``GrepMatch`` items, …) are plain
    dataclasses — not LangChain ``Serializable``s — and the filesystem
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


def dump_messages(messages: Any) -> list[Any]:
    """Serialize a sequence of LangChain messages to their ``dumpd`` form."""
    from langchain_core.load import dumpd

    return [dumpd(m) for m in messages]


def load_messages(dumped: list[Any]) -> list[Any]:
    """Rehydrate messages serialized by :func:`dump_messages`."""
    from langchain_core.load import load

    return [load(d) for d in dumped]


def tool_to_schema(tool: Any) -> dict[str, Any]:
    """Advertise a tool to the model as a full OpenAI tool schema.

    Carries name + description + argument JSON schema so the model can build
    valid arguments, not just select the tool by name.
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    return convert_to_openai_tool(tool)


# ---------------------------------------------------------------------------
# RunnableConfig strip / rebuild
# ---------------------------------------------------------------------------


def _is_jsonish(value: Any) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, (list, tuple)):
        return all(_is_jsonish(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_jsonish(v) for k, v in value.items())
    return False


def strip_runnable_config(config: Any) -> dict[str, Any]:
    """Reduce a live ``RunnableConfig`` to its JSON-safe subset for shipping.

    Keeps ``tags``, ``run_name``, ``run_id``, ``recursion_limit``, JSON-safe
    ``metadata`` and the JSON-safe (non-dunder) ``configurable`` keys. Drops
    callbacks, checkpointer / store / cache handles and every other live
    reference — those are reconstructed activity-side.
    """
    if not config:
        return {}
    out: dict[str, Any] = {}
    if config.get("tags"):
        out["tags"] = list(config["tags"])
    for key in ("run_id", "run_name"):
        if config.get(key) is not None:
            out[key] = str(config[key])
    if config.get("recursion_limit") is not None:
        out["recursion_limit"] = config["recursion_limit"]
    metadata = config.get("metadata")
    if isinstance(metadata, dict):
        out["metadata"] = {k: v for k, v in metadata.items() if _is_jsonish(v)}
    configurable = config.get("configurable")
    if isinstance(configurable, dict):
        kept = {
            k: v
            for k, v in configurable.items()
            if not k.startswith("__") and _is_jsonish(v)
        }
        if kept:
            out["configurable"] = kept
    return out


def rebuild_runnable_config(data: dict[str, Any]) -> "RunnableConfig":
    """Reconstruct a minimal ``RunnableConfig`` from :func:`strip_runnable_config`."""
    config: dict[str, Any] = {"metadata": dict(data.get("metadata", {}))}
    if data.get("tags"):
        config["tags"] = list(data["tags"])
    for key in ("run_id", "run_name"):
        if data.get(key) is not None:
            config[key] = data[key]
    if data.get("recursion_limit") is not None:
        config["recursion_limit"] = data["recursion_limit"]
    if data.get("configurable"):
        config["configurable"] = dict(data["configurable"])
    # Double cast: a TypedDict and dict[str, Any] "insufficiently overlap"
    # for basedpyright's reportInvalidCast; object is the sanctioned bridge.
    return cast("RunnableConfig", cast(object, config))


# ---------------------------------------------------------------------------
# Result cache (continue-as-new dedup)
# ---------------------------------------------------------------------------

# Per-workflow state: set at the top of the workflow run, read by the model and
# tool dispatch paths, snapshotted for continue-as-new. A ContextVar (not a
# module-global keyed by run id) so update / signal handler tasks spawned by the
# workflow inherit the same cache automatically.
_result_cache: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "_deepagents_result_cache", default=None
)


def set_result_cache(cache: dict[str, Any] | None) -> None:
    """Seed the workflow-scoped result cache (e.g. carried across CAN)."""
    _result_cache.set(dict(cache) if cache else {})


def result_cache_snapshot() -> dict[str, Any] | None:
    """Return a serializable copy of the cache, or ``None`` when empty."""
    cache = _result_cache.get()
    return dict(cache) if cache else None


def cache_key(kind: str, call_id: str, args: Any) -> str:
    """Stable key over ``(kind, call_id, args)`` for cache lookups."""
    blob = json.dumps([kind, call_id, args], default=str, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def cache_lookup(key: str) -> tuple[bool, Any]:
    """Return ``(hit, value)`` for ``key`` in the active cache."""
    cache = _result_cache.get()
    if cache is not None and key in cache:
        return True, cache[key]
    return False, None


def cache_put(key: str, value: Any) -> None:
    """Record ``value`` under ``key`` when a cache is active for this run."""
    cache = _result_cache.get()
    if cache is not None:
        cache[key] = value


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
    merged = [*_DEFAULT_PASSTHROUGH, *(user or ())]
    # dict.fromkeys preserves order while de-duplicating.
    return tuple(dict.fromkeys(merged))
