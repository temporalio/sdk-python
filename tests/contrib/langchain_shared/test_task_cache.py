"""The shared continue-as-new result cache."""

from __future__ import annotations

import functools
from contextvars import copy_context

import pytest

from temporalio.contrib._langchain._task_cache import (
    TaskResultCache,
    cache_key,
    task_id,
)


def test_instances_do_not_share_state() -> None:
    a = TaskResultCache("test_cache_a")
    b = TaskResultCache("test_cache_b")
    a.set_cache({})
    a.put("k", "va")
    assert a.lookup("k") == (True, "va")
    # b has no active cache at all — a's state is invisible to it.
    assert b.lookup("k") == (False, None)
    b.set_cache({})
    assert b.lookup("k") == (False, None)


def test_isolation_across_copied_contexts() -> None:
    cache = TaskResultCache("test_cache_ctx")

    def seed() -> None:
        cache.set_cache({"k": "inner"})

    # Setting inside a copied context must not leak to the outer context.
    copy_context().run(seed)
    assert cache.get_cache() is None


def test_set_cache_keeps_identity() -> None:
    """The langgraph plugin exposes the live dict; the core must not copy."""
    cache = TaskResultCache("test_cache_identity")
    backing: dict[str, object] = {}
    cache.set_cache(backing)
    assert cache.get_cache() is backing
    cache.put("k", 1)
    assert backing == {"k": 1}


def test_put_without_active_cache_is_a_noop() -> None:
    cache = TaskResultCache("test_cache_noop")
    cache.put("k", 1)
    assert cache.lookup("k") == (False, None)


def test_task_id_rejects_unidentifiable_functions() -> None:
    def outer():
        def inner() -> None: ...

        return inner

    with pytest.raises(ValueError, match="closures"):
        task_id(outer())

    class _MainStub:
        __module__ = "__main__"
        __qualname__ = "stub"

    with pytest.raises(ValueError, match="__main__"):
        task_id(_MainStub())

    # functools.partial instances carry neither __qualname__ nor __name__.
    with pytest.raises(ValueError, match="module level"):
        task_id(functools.partial(print))


def test_task_id_happy_path() -> None:
    assert task_id(test_task_id_happy_path) == f"{__name__}.test_task_id_happy_path"


def test_cache_key_stable_and_fallback() -> None:
    k1 = cache_key("mod.fn", (1, "a"), {"b": 2}, context=None)
    k2 = cache_key("mod.fn", (1, "a"), {"b": 2}, context=None)
    assert k1 == k2 and len(k1) == 32
    # Different inputs, different key.
    assert cache_key("mod.fn", (2, "a"), {"b": 2}) != k1

    # Unserializable arguments fall back to repr and still produce a key.
    class Weird:
        def __repr__(self) -> str:
            return "<weird>"

    k3 = cache_key("mod.fn", (Weird(),), {})
    assert len(k3) == 32


def test_langgraph_delegation_preserves_identity_semantics() -> None:
    pytest.importorskip("langgraph")
    from temporalio.contrib.langgraph import _task_cache as lg

    backing: dict[str, object] = {"seed": 1}
    lg.set_task_cache(backing)
    assert lg.get_task_cache() is backing
    lg.cache_put("k", "v")
    assert lg.cache_lookup("k") == (True, "v")
    assert backing["k"] == "v"
    lg.set_task_cache(None)
    assert lg.get_task_cache() is None
