"""Store implementation for LangGraph-Temporal integration.

This module provides ActivityLocalStore, a store implementation that captures
write operations for later replay in the Temporal workflow. It implements
the LangGraph BaseStore interface.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence

from langgraph.store.base import (
    BaseStore,
    GetOp,
    Item,
    ListNamespacesOp,
    MatchCondition,
    Op,
    PutOp,
    Result,
    SearchOp,
)

from temporalio.contrib.langgraph._models import StoreItem, StoreSnapshot, StoreWrite


class ActivityLocalStore(BaseStore):
    """Store that captures writes and serves reads from a snapshot.

    This store is used within Temporal activities to provide LangGraph nodes
    with store access. It:
    - Serves reads from a snapshot passed from the workflow
    - Captures all write operations for replay in the workflow
    - Supports read-your-writes within the same activity execution

    The captured writes are returned to the workflow, which applies them
    to its canonical store state.
    """

    def __init__(self, snapshot: StoreSnapshot) -> None:
        """Initialize the store with a snapshot.

        Args:
            snapshot: Store data snapshot from the workflow.
        """
        # Index snapshot items by (namespace, key) for fast lookup
        self._snapshot: dict[tuple[tuple[str, ...], str], dict[str, Any]] = {
            (tuple(item.namespace), item.key): item.value for item in snapshot.items
        }
        self._writes: list[StoreWrite] = []
        # Local cache for read-your-writes within this activity
        self._local_cache: dict[tuple[tuple[str, ...], str], dict[str, Any] | None] = {}

    def get_writes(self) -> list[StoreWrite]:
        """Get the list of write operations captured during execution.

        Returns:
            List of StoreWrite operations to apply to the workflow store.
        """
        return self._writes

    # =========================================================================
    # Sync Interface (BaseStore)
    # =========================================================================

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        """Execute a batch of operations.

        Args:
            ops: Iterable of store operations.

        Returns:
            List of results corresponding to each operation.
        """
        results: list[Result] = []
        for op in ops:
            if isinstance(op, GetOp):
                results.append(self._get(op.namespace, op.key))
            elif isinstance(op, PutOp):
                if op.value is None:
                    self._delete(op.namespace, op.key)
                else:
                    self._put(op.namespace, op.key, op.value)
                results.append(None)
            elif isinstance(op, SearchOp):
                results.append(self._search(op.namespace_prefix, op.filter, op.limit))
            elif isinstance(op, ListNamespacesOp):
                results.append(self._list_namespaces(op.match_conditions, op.limit))
            else:
                raise NotImplementedError(f"Operation {type(op)} not supported")
        return results

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        """Async version of batch - delegates to sync implementation.

        Args:
            ops: Iterable of store operations.

        Returns:
            List of results corresponding to each operation.
        """
        return self.batch(ops)

    # =========================================================================
    # Internal Implementation
    # =========================================================================

    def _get(self, namespace: tuple[str, ...], key: str) -> Item | None:
        """Get a single item from the store.

        Args:
            namespace: The namespace tuple.
            key: The key within the namespace.

        Returns:
            The Item if found, None otherwise.
        """
        cache_key = (namespace, key)

        # Check local cache first (read-your-writes)
        if cache_key in self._local_cache:
            cached = self._local_cache[cache_key]
            if cached is None:
                # Item was deleted
                return None
            return Item(
                value=cached,
                key=key,
                namespace=namespace,
                created_at=None,  # type: ignore[arg-type]
                updated_at=None,  # type: ignore[arg-type]
            )

        # Fall back to snapshot
        if cache_key in self._snapshot:
            return Item(
                value=self._snapshot[cache_key],
                key=key,
                namespace=namespace,
                created_at=None,  # type: ignore[arg-type]
                updated_at=None,  # type: ignore[arg-type]
            )

        return None

    def _put(
        self, namespace: tuple[str, ...], key: str, value: dict[str, Any]
    ) -> None:
        """Put a value into the store.

        Args:
            namespace: The namespace tuple.
            key: The key within the namespace.
            value: The value to store.
        """
        # Record write for workflow
        self._writes.append(
            StoreWrite(
                operation="put",
                namespace=namespace,
                key=key,
                value=value,
            )
        )
        # Update local cache for read-your-writes
        self._local_cache[(namespace, key)] = value

    def _delete(self, namespace: tuple[str, ...], key: str) -> None:
        """Delete a value from the store.

        Args:
            namespace: The namespace tuple.
            key: The key to delete.
        """
        self._writes.append(
            StoreWrite(
                operation="delete",
                namespace=namespace,
                key=key,
            )
        )
        # Mark as deleted in local cache
        self._local_cache[(namespace, key)] = None

    def _search(
        self,
        namespace_prefix: tuple[str, ...],
        filter: Optional[dict[str, Any]],
        limit: int,
    ) -> list[Item]:
        """Search for items in a namespace.

        Args:
            namespace_prefix: Namespace prefix to search within.
            filter: Optional filter conditions (not fully implemented).
            limit: Maximum number of results.

        Returns:
            List of matching Items.
        """
        results: list[Item] = []

        # Combine snapshot and local cache
        all_items: dict[tuple[tuple[str, ...], str], dict[str, Any] | None] = {
            **{k: v for k, v in self._snapshot.items()},
            **self._local_cache,
        }

        for (ns, key), value in all_items.items():
            # Skip deleted items
            if value is None:
                continue

            # Check namespace prefix match
            if len(ns) >= len(namespace_prefix) and ns[: len(namespace_prefix)] == namespace_prefix:
                # Apply filter if provided (simple equality filter)
                if filter:
                    match = all(value.get(k) == v for k, v in filter.items())
                    if not match:
                        continue

                results.append(
                    Item(
                        value=value,
                        key=key,
                        namespace=ns,
                        created_at=None,  # type: ignore[arg-type]
                        updated_at=None,  # type: ignore[arg-type]
                    )
                )

                if len(results) >= limit:
                    break

        return results

    def _list_namespaces(
        self,
        match_conditions: Optional[Sequence[MatchCondition]],
        limit: int,
    ) -> list[tuple[str, ...]]:
        """List namespaces in the store.

        Args:
            match_conditions: Optional conditions to filter namespaces.
            limit: Maximum number of results.

        Returns:
            List of namespace tuples.
        """
        namespaces: set[tuple[str, ...]] = set()

        # Collect namespaces from snapshot and local cache
        for ns, _ in self._snapshot.keys():
            namespaces.add(ns)
        for (ns, _), value in self._local_cache.items():
            if value is not None:
                namespaces.add(ns)

        # Apply match conditions if provided
        if match_conditions:
            filtered: set[tuple[str, ...]] = set()
            for ns in namespaces:
                for cond in match_conditions:
                    if cond.match_type == "prefix":
                        if len(ns) >= len(cond.path) and ns[: len(cond.path)] == tuple(
                            cond.path
                        ):
                            filtered.add(ns)
                    elif cond.match_type == "suffix":
                        if len(ns) >= len(cond.path) and ns[-len(cond.path) :] == tuple(
                            cond.path
                        ):
                            filtered.add(ns)
            namespaces = filtered

        result = list(namespaces)[:limit]
        return result
