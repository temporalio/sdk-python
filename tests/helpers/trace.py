"""Helpers for asserting trace hierarchies."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence


@dataclass
class TraceNode:
    """A trace span or run with its direct children."""

    name: str
    children: list[TraceNode] = field(default_factory=list)
    details: str | None = None


def format_trace_hierarchy(
    roots: Sequence[TraceNode], *, include_details: bool = False
) -> list[str]:
    """Render a trace forest as indented lines."""
    lines: list[str] = []

    def render(node: TraceNode, depth: int) -> None:
        line = "  " * depth + node.name
        if include_details and node.details:
            line += f" [{node.details}]"
        lines.append(line)
        for child in node.children:
            render(child, depth + 1)

    for root in roots:
        render(root, 0)
    return lines


def assert_trace_hierarchy(
    actual: Sequence[TraceNode], expected: Sequence[str]
) -> None:
    """Assert an exact trace hierarchy with detailed actual output on failure."""
    actual_tree = TraceNode("<root>", list(actual))
    expected_tree = _parse_trace(expected)
    assert _nodes_match(actual_tree, expected_tree), (
        "Trace hierarchy differed.\n"
        f"Actual:\n{chr(10).join(format_trace_hierarchy(actual, include_details=True))}\n"
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
    return (
        actual.name == expected.name
        and len(actual.children) == len(expected.children)
        and all(
            _nodes_match(actual_child, expected_child)
            for actual_child, expected_child in zip(actual.children, expected.children)
        )
    )
