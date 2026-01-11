"""Unit tests for ActivityLocalStore.

Tests for store operations: put, get, delete, search.
"""

from __future__ import annotations

from typing import cast

from langgraph.store.base import Item


class TestActivityLocalStore:
    """Tests for ActivityLocalStore."""

    def test_put_and_get(self) -> None:
        """Store should support put and get operations."""
        from langgraph.store.base import GetOp, PutOp

        from temporalio.contrib.langgraph._models import StoreSnapshot
        from temporalio.contrib.langgraph._store import ActivityLocalStore

        store = ActivityLocalStore(StoreSnapshot(items=[]))

        # Put a value
        ops = store.batch(
            [
                PutOp(
                    namespace=("user", "123"),
                    key="prefs",
                    value={"theme": "dark"},
                )
            ]
        )
        assert ops == [None]  # Put returns None

        # Get it back (read-your-writes)
        results = store.batch([GetOp(namespace=("user", "123"), key="prefs")])
        item = results[0]
        assert isinstance(item, Item)
        assert item.value == {"theme": "dark"}

        # Check writes were captured
        writes = store.get_writes()
        assert len(writes) == 1
        assert writes[0].operation == "put"
        assert writes[0].value == {"theme": "dark"}

    def test_get_from_snapshot(self) -> None:
        """Store should read from snapshot for items not in local cache."""
        from langgraph.store.base import GetOp

        from temporalio.contrib.langgraph._models import StoreItem, StoreSnapshot
        from temporalio.contrib.langgraph._store import ActivityLocalStore

        snapshot = StoreSnapshot(
            items=[
                StoreItem(
                    namespace=("user", "123"),
                    key="existing",
                    value={"from": "snapshot"},
                )
            ]
        )
        store = ActivityLocalStore(snapshot)

        results = store.batch([GetOp(namespace=("user", "123"), key="existing")])
        item = results[0]
        assert isinstance(item, Item)
        assert item.value == {"from": "snapshot"}

        # No writes since we only read
        assert store.get_writes() == []

    def test_delete(self) -> None:
        """Store should support delete operations."""
        from langgraph.store.base import GetOp, PutOp

        from temporalio.contrib.langgraph._models import StoreSnapshot
        from temporalio.contrib.langgraph._store import ActivityLocalStore

        store = ActivityLocalStore(StoreSnapshot(items=[]))

        # Put then delete
        store.batch([PutOp(namespace=("ns",), key="k", value={"v": 1})])
        store.batch([PutOp(namespace=("ns",), key="k", value=None)])  # None = delete

        # Should be deleted
        results = store.batch([GetOp(namespace=("ns",), key="k")])
        assert results[0] is None

        # Check writes include both put and delete
        writes = store.get_writes()
        assert len(writes) == 2
        assert writes[0].operation == "put"
        assert writes[1].operation == "delete"

    def test_search(self) -> None:
        """Store should support search operations."""
        from langgraph.store.base import PutOp, SearchOp

        from temporalio.contrib.langgraph._models import StoreItem, StoreSnapshot
        from temporalio.contrib.langgraph._store import ActivityLocalStore

        snapshot = StoreSnapshot(
            items=[
                StoreItem(namespace=("user", "1"), key="a", value={"v": 1}),
                StoreItem(namespace=("user", "1"), key="b", value={"v": 2}),
                StoreItem(namespace=("other",), key="c", value={"v": 3}),
            ]
        )
        store = ActivityLocalStore(snapshot)

        # Add a local write
        store.batch([PutOp(namespace=("user", "1"), key="d", value={"v": 4})])

        # Search for user/1 namespace
        results = store.batch(
            [SearchOp(namespace_prefix=("user", "1"), filter=None, limit=10)]
        )
        items = results[0]
        assert isinstance(items, list)
        assert len(items) == 3  # a, b, d (not c which is in different namespace)

    def test_local_writes_override_snapshot(self) -> None:
        """Local writes should override values from snapshot."""
        from langgraph.store.base import GetOp, PutOp

        from temporalio.contrib.langgraph._models import StoreItem, StoreSnapshot
        from temporalio.contrib.langgraph._store import ActivityLocalStore

        snapshot = StoreSnapshot(
            items=[
                StoreItem(
                    namespace=("user", "1"),
                    key="pref",
                    value={"theme": "light"},
                )
            ]
        )
        store = ActivityLocalStore(snapshot)

        # Read original value
        results = store.batch([GetOp(namespace=("user", "1"), key="pref")])
        item = results[0]
        assert isinstance(item, Item)
        assert item.value == {"theme": "light"}

        # Override with local write
        store.batch(
            [PutOp(namespace=("user", "1"), key="pref", value={"theme": "dark"})]
        )

        # Should return new value
        results = store.batch([GetOp(namespace=("user", "1"), key="pref")])
        item = results[0]
        assert isinstance(item, Item)
        assert item.value == {"theme": "dark"}

    def test_get_nonexistent_returns_none(self) -> None:
        """Getting nonexistent key should return None."""
        from langgraph.store.base import GetOp

        from temporalio.contrib.langgraph._models import StoreSnapshot
        from temporalio.contrib.langgraph._store import ActivityLocalStore

        store = ActivityLocalStore(StoreSnapshot(items=[]))

        results = store.batch([GetOp(namespace=("user", "1"), key="missing")])
        assert results[0] is None

    def test_batch_multiple_operations(self) -> None:
        """Store should handle multiple operations in a single batch."""
        from langgraph.store.base import GetOp, PutOp

        from temporalio.contrib.langgraph._models import StoreSnapshot
        from temporalio.contrib.langgraph._store import ActivityLocalStore

        store = ActivityLocalStore(StoreSnapshot(items=[]))

        # Batch multiple puts
        results = store.batch(
            [
                PutOp(namespace=("ns",), key="a", value={"v": 1}),
                PutOp(namespace=("ns",), key="b", value={"v": 2}),
                PutOp(namespace=("ns",), key="c", value={"v": 3}),
            ]
        )
        assert results == [None, None, None]

        # Batch multiple gets
        results = store.batch(
            [
                GetOp(namespace=("ns",), key="a"),
                GetOp(namespace=("ns",), key="b"),
                GetOp(namespace=("ns",), key="c"),
            ]
        )
        # Use cast to satisfy type checker - we know these are Item objects
        assert cast(Item, results[0]).value == {"v": 1}
        assert cast(Item, results[1]).value == {"v": 2}
        assert cast(Item, results[2]).value == {"v": 3}
