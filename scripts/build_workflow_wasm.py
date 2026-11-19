"""Build a Python workflow bundle from an SDK checkout and locked local dependencies."""

import argparse
import ast
import hashlib
import importlib.metadata
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Iterable
from pathlib import Path

COMPONENTIZER_VERSION = "0.25.1"
SDK_ROOT = Path(__file__).resolve().parents[1]


def _sources(root: Path) -> Iterable[tuple[str, Path]]:
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if (
            not any(part.startswith(".") for part in relative.parts)
            and not path.is_symlink()
        ):
            yield relative.as_posix(), path


def build(
    output: Path,
    workflows: list[str],
    source: Path,
    dependencies: list[Path],
    core: Path,
) -> None:
    """Package source without importing the workflow modules on the build host."""
    for root in [source, *dependencies]:
        if not root.is_dir():
            raise ValueError(f"Bundle input must be a directory: {root}")
    version = importlib.metadata.version("componentize-py")
    if version != COMPONENTIZER_VERSION:
        raise ValueError(
            f"Expected componentize-py {COMPONENTIZER_VERSION}, found {version}"
        )
    entries = []
    for entry in workflows:
        module, _, class_name = entry.partition(":")
        if not all(
            part.isidentifier()
            for name in (module, class_name)
            for part in name.split(".")
        ):
            raise ValueError("Workflow entries must have the form module:Class")
        entries.append((module, class_name))
    version_assignment = next(
        node
        for node in ast.parse((SDK_ROOT / "temporalio/service.py").read_bytes()).body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        )
    )
    sdk_version = ast.literal_eval(version_assignment.value)
    with tempfile.TemporaryDirectory(prefix="temporal-workflow-wasm-") as tmp:
        stage = Path(tmp)
        trusted = stage / "trusted"
        for name, path in _sources(SDK_ROOT / "temporalio"):
            if name.startswith("bridge/sdk-core/"):
                continue
            dest = trusted / "temporalio" / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
        # The guest imports only the workflow runtime from worker, and never needs
        # the service client or its native extension to discover the SDK version.
        (trusted / "temporalio/__init__.py").write_text(
            f"__version__ = {sdk_version!r}\n"
        )
        (trusted / "temporalio/worker/__init__.py").write_text("")
        archive_file = io.BytesIO()
        with zipfile.ZipFile(archive_file, "w", zipfile.ZIP_DEFLATED) as archive:
            for dependency in dependencies:
                for name, path in _sources(dependency):
                    if (
                        name.startswith(("google/protobuf/", "nexusrpc/"))
                        or name == "typing_extensions.py"
                    ):
                        dest = trusted / name
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(path, dest)
                    else:
                        archive.writestr(name, path.read_bytes())
            # SDK and generated protobuf modules can also be imported lazily.
            for name, path in _sources(trusted):
                archive.writestr(name, path.read_bytes())
            for name, path in _sources(source):
                if name.startswith("temporalio/"):
                    raise ValueError(
                        "Workflow source cannot replace the Temporal guest SDK"
                    )
                archive.writestr(name, path.read_bytes())
        if (
            not (trusted / "google/protobuf").is_dir()
            or not (trusted / "nexusrpc").is_dir()
            or not (trusted / "typing_extensions.py").is_file()
        ):
            raise ValueError(
                "Dependency paths must include protobuf, nexus-rpc, and typing-extensions"
            )
        (trusted / "temporal_wasm_bundle_data.py").write_text(
            f"ARCHIVE = {archive_file.getvalue()!r}\nWORKFLOWS = {entries!r}\n"
        )
        shutil.copyfile(
            SDK_ROOT / "scripts/wasm/guest.py", trusted / "temporal_wasm_guest.py"
        )
        env = os.environ.copy()
        env.pop("VIRTUAL_ENV", None)
        env.pop("PYTHONPATH", None)
        subprocess.run(
            [
                str(Path(sys.executable).with_name("componentize-py")),
                "-d",
                str(core / "crates/workflow-wasm/wit"),
                "componentize",
                "--stub-wasi",
                "-p",
                str(trusted),
                "temporal_wasm_guest",
                "-o",
                str(output.resolve()),
            ],
            cwd=trusted,
            env=env,
            check=True,
        )
    output.with_suffix(output.suffix + ".json").write_text(
        json.dumps(
            {
                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "componentize-py": version,
                "guest-python": "3.14",
                "sdk-python": sdk_version,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--workflow", action="append", required=True)
    parser.add_argument("--dependency-path", type=Path, action="append", required=True)
    parser.add_argument(
        "--core", type=Path, default=SDK_ROOT / "temporalio/bridge/sdk-core"
    )
    args = parser.parse_args()
    build(
        args.output,
        args.workflow,
        args.source.resolve(),
        [path.resolve() for path in args.dependency_path],
        args.core.resolve(),
    )
