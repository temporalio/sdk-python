"""Invoke the shared, pinned sdk-rust changelog tooling."""

from __future__ import annotations

import dataclasses
import datetime
import json
import pathlib
import subprocess
from collections.abc import Sequence


@dataclasses.dataclass(frozen=True)
class ReleasePlan:
    """Validated changelog output and repository-relative fragments to consume."""

    changelog: str
    consumed_paths: tuple[str, ...]


def run_tool(repo_root: pathlib.Path, args: Sequence[str]) -> str:
    """Run the pinned tool, retaining diagnostics on failure."""
    core = repo_root / "temporalio/bridge/sdk-core"
    if not (core / ".git").exists():
        raise RuntimeError("Initialize sdk-rust with 'git submodule update --init'")
    try:
        result = subprocess.run(
            [
                "cargo",
                "run",
                "--quiet",
                "--manifest-path",
                str(core / "crates/changelog-release-notes/Cargo.toml"),
                "--bin",
                "changelog-tool",
                "--",
                *args,
                "--repo",
                str(repo_root.resolve()),
            ],
            cwd=core,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as err:
        raise RuntimeError(f"Shared changelog tool failed:\n{err.stderr}") from err
    return result.stdout


def prepare_changelog(
    repo_root: pathlib.Path, version: str, release_date: datetime.date
) -> ReleasePlan:
    """Calculate release notes before modifying SDK files."""
    payload = json.loads(
        run_tool(
            repo_root,
            [
                "prepare",
                "--version",
                version,
                "--date",
                release_date.isoformat(),
                "--breaking-heading",
                ":boom: Breaking Changes",
            ],
        )
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("changelog"), str):
        raise RuntimeError("Invalid changelog release plan")
    paths = payload.get("consumed_paths")
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise RuntimeError("Invalid consumed fragment paths")
    categories = {
        "added",
        "changed",
        "deprecated",
        "breaking-changes",
        "fixed",
        "security",
    }
    for path in paths:
        parts = pathlib.PurePosixPath(path).parts
        if (
            len(parts) != 3
            or parts[0] != "changelog"
            or parts[1] not in categories
            or pathlib.PurePosixPath(path).suffix != ".md"
            or ".." in parts
        ):
            raise RuntimeError(f"Invalid consumed fragment path: {path!r}")
    if len(set(paths)) != len(paths):
        raise RuntimeError("Duplicate consumed fragment paths")
    return ReleasePlan(payload["changelog"], tuple(paths))
