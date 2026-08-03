"""The shared ``RunnableConfig`` strip/rebuild.

The langgraph plugin's strip output is RELEASED behavior: stripped configs
ride in activity payloads and feed continue-as-new cache keys, so the shared
implementation must reproduce it byte-for-byte. ``_reference_strip_pre_refactor``
below is the pre-refactor langgraph implementation embedded verbatim; the
corpus test string-compares JSON dumps (captures key order, not just dict
equality) and compares derived cache keys.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from temporalio.contrib._langchain._runnable_config import (
    is_jsonish,
    rebuild_runnable_config,
    strip_runnable_config,
)
from temporalio.contrib._langchain._task_cache import cache_key

pytest.importorskip("langgraph")

from langgraph._internal._constants import (  # noqa: E402  # pyright: ignore[reportMissingTypeStubs]
    CONFIG_KEY_CHECKPOINT_ID,
    CONFIG_KEY_CHECKPOINT_MAP,
    CONFIG_KEY_CHECKPOINT_NS,
    CONFIG_KEY_DURABILITY,
    CONFIG_KEY_RESUMING,
    CONFIG_KEY_TASK_ID,
    CONFIG_KEY_THREAD_ID,
)

_KEPT = (
    CONFIG_KEY_CHECKPOINT_NS,
    CONFIG_KEY_CHECKPOINT_ID,
    CONFIG_KEY_CHECKPOINT_MAP,
    CONFIG_KEY_THREAD_ID,
    CONFIG_KEY_TASK_ID,
    CONFIG_KEY_RESUMING,
    CONFIG_KEY_DURABILITY,
)


def _reference_strip_pre_refactor(config: Any) -> dict[str, Any]:
    """The langgraph plugin's strip_runnable_config as released (verbatim)."""
    orig = config or {}
    configurable = orig.get("configurable") or {}

    result: dict[str, Any] = {
        "tags": list(orig.get("tags") or []),
        "metadata": dict(orig.get("metadata") or {}),
    }
    if run_name := orig.get("run_name"):
        result["run_name"] = run_name
    if run_id := orig.get("run_id"):
        result["run_id"] = run_id
    if (recursion_limit := orig.get("recursion_limit")) is not None:
        result["recursion_limit"] = recursion_limit

    stripped_configurable: dict[str, Any] = {
        key: configurable[key]
        for key in (
            CONFIG_KEY_CHECKPOINT_NS,
            CONFIG_KEY_CHECKPOINT_ID,
            CONFIG_KEY_CHECKPOINT_MAP,
            CONFIG_KEY_THREAD_ID,
            CONFIG_KEY_TASK_ID,
            CONFIG_KEY_RESUMING,
            CONFIG_KEY_DURABILITY,
        )
        if key in configurable
    }
    if stripped_configurable:
        result["configurable"] = stripped_configurable
    return result


class _LiveJunk:
    """Stand-in for callbacks / checkpointer handles / pregel callables."""

    def __repr__(self) -> str:
        return "<live-junk>"


def _corpus() -> list[Any]:
    junk = _LiveJunk()
    scrambled_configurable = {
        "user_key": "kept-by-nothing",
        CONFIG_KEY_DURABILITY: "sync",
        "__pregel_send": junk,
        CONFIG_KEY_THREAD_ID: "t-1",
        CONFIG_KEY_CHECKPOINT_NS: "ns",
        "another": {"nested": True},
        CONFIG_KEY_RESUMING: False,
        CONFIG_KEY_CHECKPOINT_MAP: {"a": "b"},
        CONFIG_KEY_TASK_ID: "task-9",
        CONFIG_KEY_CHECKPOINT_ID: "ckpt-3",
    }
    return [
        None,
        {},
        {"tags": [], "metadata": {}},
        {"tags": ["a", "b"], "metadata": {"k": "v", "obj": junk}},
        {"run_name": ""},  # falsy run_name: dropped
        {"run_name": "r", "run_id": uuid.uuid4()},
        {"recursion_limit": 0},  # zero is kept (not-None gate)
        {"recursion_limit": None},
        {
            "tags": ["x"],
            "metadata": {"m": 1},
            "run_name": "full",
            "run_id": "rid",
            "recursion_limit": 25,
            "callbacks": junk,
            "configurable": scrambled_configurable,
        },
        {"configurable": {k: f"v-{k}" for k in reversed(_KEPT)}},
        {"configurable": {"only": "dropped keys"}},
    ]


def test_langgraph_shape_is_byte_identical_to_released_behavior() -> None:
    for cfg in _corpus():
        expected = _reference_strip_pre_refactor(cfg)
        actual = strip_runnable_config(cfg, configurable_keys=_KEPT)
        assert json.dumps(actual, default=str) == json.dumps(expected, default=str), cfg
        # Cache keys derived from stripped configs must not shift either.
        assert cache_key("t", (), {"config": actual}) == cache_key(
            "t", (), {"config": expected}
        )


def test_langgraph_plugin_wrapper_matches_reference() -> None:
    from temporalio.contrib.langgraph._langgraph_config import (
        strip_runnable_config as lg_strip,
    )

    for cfg in _corpus():
        assert json.dumps(lg_strip(cfg), default=str) == json.dumps(
            _reference_strip_pre_refactor(cfg), default=str
        )


def test_filter_mode_matches_deepagents_semantics() -> None:
    junk = _LiveJunk()
    cfg = {
        "tags": ["t"],
        "metadata": {"ok": 1, "bad": junk},
        "run_id": "rid",
        "configurable": {"keep": "v", "__dunder": "x", "unsafe": junk},
    }
    out = strip_runnable_config(
        cfg,
        configurable_filter=lambda k, v: not k.startswith("__") and is_jsonish(v),
        metadata_filter=is_jsonish,
    )
    assert out == {
        "tags": ["t"],
        "metadata": {"ok": 1},
        "run_id": "rid",
        "configurable": {"keep": "v"},
    }


def test_rebuild_round_trip() -> None:
    stripped = {
        "tags": ["t"],
        "metadata": {"m": 1},
        "run_id": "rid",
        "recursion_limit": 5,
        "configurable": {"k": "v"},
    }
    assert rebuild_runnable_config(dict(stripped)) == {
        "metadata": {"m": 1},
        "tags": ["t"],
        "run_id": "rid",
        "recursion_limit": 5,
        "configurable": {"k": "v"},
    }
    # Empty input still yields the always-present metadata mapping.
    assert rebuild_runnable_config({}) == {"metadata": {}}


def test_exactly_one_configurable_selector_required() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        strip_runnable_config({})
    with pytest.raises(ValueError, match="exactly one"):
        strip_runnable_config(
            {}, configurable_keys=("a",), configurable_filter=lambda k, v: True
        )
