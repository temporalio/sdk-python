"""Shared test helpers for LangSmith plugin tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock


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

    # Strict: reject dangling parent references
    known_ids = {r.id for r in runs}
    for r in runs:
        if r.parent_run_id is not None and r.parent_run_id not in known_ids:
            raise AssertionError(
                f"Run {r.name!r} (id={r.id}) has parent_run_id={r.parent_run_id} "
                f"which is not in the collected runs — dangling parent reference"
            )
    # Only walk true roots (parent_run_id is None)
    _walk(None, 0)

    return result


def make_mock_ls_client(collector: InMemoryRunCollector) -> MagicMock:
    """Create a mock langsmith.Client wired to a collector."""
    client = MagicMock()
    client.create_run.side_effect = collector.record_create
    client.update_run.side_effect = collector.record_update
    client.session = MagicMock()
    client.tracing_queue = MagicMock()
    return client
