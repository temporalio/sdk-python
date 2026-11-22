"""Render the workflow source call graph alongside the actual replay digests."""

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path

from scripts.build_workflow_wasm import (
    _discover_addressable_functions,
    _SourceCallGraph,
)


@dataclass(frozen=True)
class _Revision:
    version: int
    digests: dict[str, tuple[str, str | None]]
    calls: dict[str, list[str]]


class RolloutCallGraph:
    def __init__(self, source: Path, workflow: tuple[str, str]) -> None:
        self._source = source
        self._workflow = workflow
        self._revisions: list[_Revision] = []

    def record(self, version: int, bindings: dict[str, bytes]) -> None:
        graph = _SourceCallGraph()
        _discover_addressable_functions(
            self._source, [self._workflow], call_graph=graph
        )
        module, workflow_class = self._workflow
        pending = [f"{module}:{workflow_class}.run"]
        digests: dict[str, tuple[str, str | None]] = {}
        calls: dict[str, list[str]] = {}
        while pending:
            name = pending.pop()
            if name in digests:
                continue
            definition = ast.unparse(graph.definitions[name]).encode()
            digest = hashlib.sha256(definition).hexdigest()
            capsule = bindings[name].hex() if name in bindings else None
            digests[name] = (digest, capsule)
            targets = graph.calls.get(name, [])
            calls[name] = targets
            pending.extend(reversed(targets))
        self._revisions.append(_Revision(version, digests, calls))

    def write(self, path: Path) -> None:
        diagram = ["flowchart LR"]
        rows = [
            "| Version | Function | Source SHA-256 | Replay capsule SHA-256 |",
            "|---|---|---|---|",
        ]
        previous: dict[str, tuple[str, str | None]] = {}
        changed = []
        for revision in self._revisions:
            version = revision.version
            ids = {
                name: f"v{version}_{index}"
                for index, name in enumerate(revision.digests)
            }
            diagram.extend(
                [
                    f'    subgraph v{version}["Version {version}"]',
                    "        direction TB",
                ]
            )
            for name, (source, capsule) in revision.digests.items():
                label = f"{name}<br/>source {source[:12]}"
                if capsule is not None:
                    label += f"<br/>capsule {capsule[:12]}"
                if previous and previous.get(name) != (source, capsule):
                    label += "<br/>changed"
                    changed.append(ids[name])
                diagram.append(f'        {ids[name]}["{label}"]')
                replay = f"`{capsule}`" if capsule is not None else "—"
                rows.append(f"| {version} | `{name}` | `{source}` | {replay} |")
            for caller, targets in revision.calls.items():
                positions: dict[str, list[str]] = {}
                for index, callee in enumerate(targets, start=1):
                    positions.setdefault(callee, []).append(str(index))
                for callee, indices in positions.items():
                    label = ", ".join(indices)
                    diagram.append(f"        {ids[caller]} -->|{label}| {ids[callee]}")
            diagram.append("    end")
            previous = revision.digests
        if changed:
            diagram.extend(
                [
                    "    classDef changed stroke-width:3px",
                    f"    class {','.join(changed)} changed",
                ]
            )
        mermaid = "\n".join(["```mermaid", *diagram, "```"])
        path.write_text(
            "\n".join(
                [
                    "# Workflow rollout call graph",
                    "",
                    "Source hashes fingerprint individual Python definitions. Replay capsule"
                    " hashes come from the published slot bindings.",
                    "",
                    "Edge numbers show the order of statically resolved call sites within"
                    " each caller. Branch and loop execution depends on the workflow run.",
                    "",
                    mermaid,
                    "",
                    "## Full digests",
                    "",
                    *rows,
                    "",
                ]
            ),
            encoding="utf-8",
        )
        print(f"Call graph at {path}\n{mermaid}")
