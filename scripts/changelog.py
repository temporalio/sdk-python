"""Invoke the shared, pinned sdk-rust changelog tooling."""

from __future__ import annotations

import datetime
import pathlib
import subprocess
from collections.abc import Sequence


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
) -> None:
    """Write the dated changelog and consume fragments using the shared tool."""
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
