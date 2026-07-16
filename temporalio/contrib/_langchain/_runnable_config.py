"""``RunnableConfig`` strip/rebuild shared by the LangChain-family plugins."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig


def is_jsonish(value: Any) -> bool:
    """Return True for values that are JSON-serializable with the standard encoder."""
        return True
    if isinstance(value, (list, tuple)):
        return all(is_jsonish(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and is_jsonish(v) for k, v in value.items())
    return False


def strip_runnable_config(
    config: Mapping[str, Any] | None,
    *,
    configurable_keys: Sequence[str] | None = None,
    configurable_filter: Callable[[str, Any], bool] | None = None,
    metadata_filter: Callable[[Any], bool] | None = None,
) -> dict[str, Any]:
    """Return a serializable subset of a ``RunnableConfig``.

    The full object holds non-serializable things (callbacks, checkpointer /
    store / cache handles, pregel send/read callables) that cannot cross an
    activity boundary, so only primitive fields and a caller-selected subset
    of ``configurable`` are kept. The output shape follows the langgraph
    plugin's released behavior: ``tags`` and ``metadata`` are always present
    (empty when absent), ``run_name`` / ``run_id`` are kept when truthy,
    ``recursion_limit`` when not None, and ``configurable`` only when the
    selection is non-empty.

    Exactly one of ``configurable_keys`` (keys kept in whitelist order) or
    ``configurable_filter`` (entries kept in source order) must be provided.
    ``metadata_filter`` optionally drops metadata VALUES (default: keep all,
    matching langgraph).
    """
    if (configurable_keys is None) == (configurable_filter is None):
        raise ValueError(
            "exactly one of configurable_keys or configurable_filter is required"
        )
    orig: Mapping[str, Any] = config or {}
    configurable: Mapping[str, Any] = orig.get("configurable") or {}

    metadata = dict(orig.get("metadata") or {})
    if metadata_filter is not None:
        metadata = {k: v for k, v in metadata.items() if metadata_filter(v)}
    result: dict[str, Any] = {
        "tags": list(orig.get("tags") or []),
        "metadata": metadata,
    }
    if run_name := orig.get("run_name"):
        result["run_name"] = run_name
    if run_id := orig.get("run_id"):
        result["run_id"] = run_id
    if (recursion_limit := orig.get("recursion_limit")) is not None:
        result["recursion_limit"] = recursion_limit

    if configurable_keys is not None:
        stripped_configurable: dict[str, Any] = {
            key: configurable[key] for key in configurable_keys if key in configurable
        }
    else:
        stripped_configurable = {
            k: v
            for k, v in configurable.items()
            # The guard above makes configurable_filter non-None on this
            # branch; the re-check narrows for type checkers.
            if configurable_filter is not None and configurable_filter(k, v)
        }
    if stripped_configurable:
        result["configurable"] = stripped_configurable
    return result


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
