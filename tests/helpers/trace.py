"""Helpers for asserting indented trace hierarchies."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass
class TraceNode:
    name: str
    children: list[TraceNode] = field(default_factory=list)


def format_trace_hierarchy(roots: Sequence[TraceNode]) -> list[str]:
    """Render a trace forest as indented lines."""
    lines: list[str] = []

    def render(node: TraceNode, depth: int) -> None:
        lines.append("  " * depth + node.name)
        for child in node.children:
            render(child, depth + 1)

    for root in roots:
        render(root, 0)
    return lines


def assert_trace_hierarchy(
    actual: Sequence[TraceNode], expected: Sequence[str]
) -> None:
    """Assert a trace hierarchy, allowing matching Start/Run siblings to swap.

    Activity execution can begin before the asynchronously published Start span
    reaches an in-memory exporter. The two spans retain the same parent, so this
    accepts only that sibling transposition and preserves all other ordering.
    """
    actual_tree = TraceNode("<root>", list(actual))
    expected_tree = _parse_trace(expected)
    assert _nodes_match(actual_tree, expected_tree), (
        "Trace hierarchy differed.\n"
        f"Actual:\n{chr(10).join(format_trace_hierarchy(actual))}\n"
        f"Expected:\n{chr(10).join(expected)}"
    )


def _parse_trace(trace: Sequence[str]) -> TraceNode:
    root = TraceNode("<root>")
    stack: list[tuple[int, TraceNode]] = [(-1, root)]
    for line in trace:
        name = line.lstrip()
        indent = len(line) - len(name)
        assert name and indent % 2 == 0, f"Invalid trace line: {line!r}"
        depth = indent // 2
        while stack[-1][0] >= depth:
            stack.pop()
        assert depth == stack[-1][0] + 1, f"Invalid trace nesting: {line!r}"
        node = TraceNode(name)
        stack[-1][1].children.append(node)
        stack.append((depth, node))
    return root


def _nodes_match(actual: TraceNode, expected: TraceNode) -> bool:
    if actual.name != expected.name or len(actual.children) != len(expected.children):
        return False

    index = 0
    while index < len(actual.children):
        actual_child = actual.children[index]
        expected_child = expected.children[index]
        if _nodes_match(actual_child, expected_child):
            index += 1
            continue

        if index + 1 == len(actual.children):
            return False
        actual_pair = actual.children[index : index + 2]
        expected_pair = expected.children[index : index + 2]
        if not _is_start_run_pair(*actual_pair) or not _is_start_run_pair(
            *expected_pair
        ):
            return False
        expected_by_name = {node.name: node for node in expected_pair}
        if any(node.name not in expected_by_name for node in actual_pair):
            return False
        if not all(
            _nodes_match(node, expected_by_name[node.name]) for node in actual_pair
        ):
            return False
        index += 2

    return True


def _is_start_run_pair(first: TraceNode, second: TraceNode) -> bool:
    first_start = _start_run_suffix(first.name)
    second_start = _start_run_suffix(second.name)
    first_run = _run_suffix(first.name)
    second_run = _run_suffix(second.name)
    return (first_start is not None and first_start == second_run) or (
        second_start is not None and second_start == first_run
    )


def _start_run_suffix(name: str) -> str | None:
    return name.removeprefix("Start") if name.startswith("Start") else None


def _run_suffix(name: str) -> str | None:
    return name.removeprefix("Run") if name.startswith("Run") else None
