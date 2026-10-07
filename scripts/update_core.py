"""Update SDK Core and synchronize the Python bridge's Cargo dependencies."""

from __future__ import annotations

import pathlib
import subprocess
import sys
from collections.abc import MutableMapping, Sequence
from typing import Any, cast

import tomlkit

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts.changelog import run_tool


def update_bridge_dependencies(repo_root: pathlib.Path) -> None:
    """Copy Core path dependency versions and refresh the bridge lockfile."""
    bridge = repo_root / "temporalio/bridge"
    core = (bridge / "sdk-core").resolve()
    manifest_path = bridge / "Cargo.toml"
    original = manifest_path.read_text()
    manifest = tomlkit.parse(original)
    dependencies = cast(MutableMapping[str, Any], manifest["dependencies"])
    for name, dependency in dependencies.items():
        if not isinstance(dependency, MutableMapping):
            continue
        path = dependency.get("path")
        requirement = dependency.get("version")
        if not isinstance(path, str):
            continue
        crate = (bridge / path).resolve()
        if not crate.is_relative_to(core):
            continue
        package = cast(
            MutableMapping[str, Any],
            tomlkit.parse((crate / "Cargo.toml").read_text())["package"],
        )
        version = cast(str, package["version"])
        if requirement != version:
            dependency["version"] = version
            print(f"Updated {name} requirement: {requirement} -> {version}")
    updated = tomlkit.dumps(manifest)
    if updated != original:
        manifest_path.write_text(updated)
    subprocess.run(["cargo", "fetch"], cwd=bridge, check=True)


def update_core(repo_root: pathlib.Path, args: Sequence[str]) -> None:
    """Import Core changes before updating Python's bridge manifest and lockfile."""
    print(
        run_tool(
            repo_root,
            ["update-core", "--submodule", "temporalio/bridge/sdk-core", *args],
        ),
        end="",
        flush=True,
    )
    update_bridge_dependencies(repo_root)


if __name__ == "__main__":
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        print(__doc__)
        print(run_tool(repo_root, ["update-core", "--help"]), end="")
    else:
        update_core(repo_root, args)
