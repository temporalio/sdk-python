"""Update SDK Core, bridge dependencies, and generated protobuf bindings."""

from __future__ import annotations

import argparse
import pathlib
import re
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
    subprocess.run(["cargo", "update"], cwd=bridge, check=True)


def latest_core_tag(repo_root: pathlib.Path) -> str:
    """Fetch Core tags and select the highest stable Core version."""
    core = repo_root / "temporalio/bridge/sdk-core"
    if not (core / ".git").exists():
        raise RuntimeError("Initialize sdk-core with 'git submodule update --init'")
    subprocess.run(["git", "fetch", "origin", "--tags"], cwd=core, check=True)
    result = subprocess.run(
        ["git", "tag", "--list", "core-v*"],
        cwd=core,
        check=True,
        capture_output=True,
        text=True,
    )
    versions = {
        tag: tuple(map(int, match.groups()))
        for tag in result.stdout.splitlines()
        if (match := re.fullmatch(r"core-v(\d+)\.(\d+)\.(\d+)", tag))
    }
    if not versions:
        raise RuntimeError("No stable core-v* release tags found in sdk-core")
    return max(versions, key=versions.__getitem__)


def update_core(repo_root: pathlib.Path, args: Sequence[str]) -> None:
    """Import Core changes, refresh bridge dependencies, and generate protos."""
    parser = argparse.ArgumentParser(add_help=False)
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--latest-tag", action="store_true")
    target.add_argument("--revision")
    options, args = parser.parse_known_args(args)
    if options.latest_tag:
        args.extend(["--revision", latest_core_tag(repo_root)])
    elif options.revision is not None:
        args.extend(["--revision", options.revision])
    print(
        run_tool(
            repo_root,
            ["update-core", "--submodule", "temporalio/bridge/sdk-core", *args],
        ),
        end="",
        flush=True,
    )
    update_bridge_dependencies(repo_root)
    subprocess.run(["poe", "gen-protos"], cwd=repo_root, check=True)


if __name__ == "__main__":
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        print(__doc__)
        print(
            "Defaults to the fetched origin/main head. Use --latest-tag to select "
            "the highest stable core-vX.Y.Z tag instead (cannot combine with --revision)."
        )
        print(run_tool(repo_root, ["update-core", "--help"]), end="")
    else:
        update_core(repo_root, args)
