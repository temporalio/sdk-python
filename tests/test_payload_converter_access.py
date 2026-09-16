"""Keep raw payload conversion confined to intentional SDK boundaries."""

import ast
import os
from pathlib import Path

# These expressions belong to public APIs, other converter owners, or restricted value domains.
_ALLOWED_READS = {
    (
        "contrib/workflow_streams/_stream.py",
        "workflow.payload_converter",
    ): "Workflow Streams uses the public current-converter API for user items.",
    (
        "contrib/opentelemetry/_interceptor.py",
        "self.payload_converter",
    ): "Tracing interceptors own a converter for header dictionaries.",
    (
        "converter/_search_attributes.py",
        "default().payload_converter",
    ): "Search attributes only support their own restricted set of primitive values.",
    (
        "testing/_activity.py",
        "env.payload_converter",
    ): "ActivityEnvironment exposes its own configurable converter to activity context.",
}


def test_sdk_payload_converter_access() -> None:
    root = Path(__file__).resolve().parents[1] / "temporalio"
    violations = []
    for directory, subdirectories, filenames in os.walk(root):
        subdirectories[:] = [
            name
            for name in subdirectories
            if name not in {"__pycache__", "target", "sdk-core"}
        ]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = Path(directory) / filename
            source = path.read_text(encoding="utf-8")
            lines = source.splitlines()
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Attribute)
                    and node.attr == "payload_converter"
                    and isinstance(node.ctx, ast.Load)
                ):
                    continue
                relative_path = path.relative_to(root).as_posix()
                if (relative_path, ast.unparse(node)) in _ALLOWED_READS:
                    continue
                _, marker, reason = lines[node.lineno - 1].partition(
                    "# raw-payload-converter:"
                )
                if marker and reason.strip():
                    continue
                violations.append(f"{relative_path}:{node.lineno}: {ast.unparse(node)}")
    assert not violations, (
        "Use DataConverter._get_internal_payload_converter() for SDK serialization. "
        "Intentional raw access requires '# raw-payload-converter: <reason>'.\n"
        + "\n".join(sorted(violations))
    )
