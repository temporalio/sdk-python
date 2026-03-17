"""Shared test fixtures for LangSmith plugin tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest


@dataclass
class _RunRecord:
    """A single recorded run."""

    id: str
    parent_run_id: str | None
    name: str
    run_type: str
    inputs: dict[str, Any]
    outputs: dict[str, Any] | None = None
    error: str | None = None


class InMemoryRunCollector:
    """Collects runs from a mock LangSmith client.

    Each call to create_run / update_run appends or updates an entry.
    """

    def __init__(self) -> None:
        self.runs: list[_RunRecord] = []
        self._by_id: dict[str, _RunRecord] = {}

    def record_create(self, **kwargs: Any) -> None:
        rec = _RunRecord(
            id=str(kwargs.get("id", kwargs.get("run_id", ""))),
            parent_run_id=(
                str(kwargs["parent_run_id"]) if kwargs.get("parent_run_id") else None
            ),
            name=kwargs.get("name", ""),
            run_type=kwargs.get("run_type", "chain"),
            inputs=kwargs.get("inputs", {}),
        )
        self.runs.append(rec)
        self._by_id[rec.id] = rec

    def record_update(self, run_id: str, **kwargs: Any) -> None:
        run_id_str = str(run_id)
        rec = self._by_id.get(run_id_str)
        if rec is None:
            return
        if "outputs" in kwargs:
            rec.outputs = kwargs["outputs"]
        if "error" in kwargs:
            rec.error = kwargs["error"]

    def clear(self) -> None:
        self.runs.clear()
        self._by_id.clear()


def dump_runs(collector: InMemoryRunCollector) -> list[str]:
    """Reconstruct parent-child hierarchy from collected runs.

    Returns a list of indented strings, e.g.:
        ["StartWorkflow:MyWf", "  RunWorkflow:MyWf", "    StartActivity:do_thing"]
    """
    runs = collector.runs
    children: dict[str | None, list[_RunRecord]] = {}
    for r in runs:
        children.setdefault(r.parent_run_id, []).append(r)

    result: list[str] = []

    def _walk(parent_id: str | None, depth: int) -> None:
        for child in children.get(parent_id, []):
            result.append("  " * depth + child.name)
            _walk(child.id, depth + 1)

    # Roots: runs whose parent_run_id is None or not in our set
    known_ids = {r.id for r in runs}
    root_parents = {
        r.parent_run_id
        for r in runs
        if r.parent_run_id is None or r.parent_run_id not in known_ids
    }
    for rp in sorted(root_parents, key=lambda x: (x is not None, x)):
        _walk(rp, 0)

    return result


@pytest.fixture
def collector() -> InMemoryRunCollector:
    return InMemoryRunCollector()


@pytest.fixture
def mock_ls_client(collector: InMemoryRunCollector) -> MagicMock:
    """A mock langsmith.Client that records create_run / update_run calls."""
    client = MagicMock()
    client.create_run.side_effect = collector.record_create
    client.update_run.side_effect = collector.record_update
    # Stub session property (needed by RunTree internals)
    client.session = MagicMock()
    client.tracing_queue = MagicMock()
    return client


@pytest.fixture
def langsmith_plugin(mock_ls_client: MagicMock, collector: InMemoryRunCollector):
    """Return (plugin, collector) wired to a mock client."""
    from temporalio.contrib.langsmith import LangSmithPlugin

    plugin = LangSmithPlugin(client=mock_ls_client)
    return plugin, collector
