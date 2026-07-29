from __future__ import annotations

import datetime
import pathlib
import subprocess

import pytest

from scripts.prepare_release import (
    create_release_branch,
    create_release_pr,
    ensure_clean_worktree,
    ensure_only_release_changes,
    finalize_changelog_release,
    push_release_branch,
    replace_project_version,
    replace_service_version,
)


def test_finalize_changelog_release_seeds_unreleased_and_versions_notes() -> None:
    changelog = """# Changelog

## [Unreleased]

### Added

### Changed

- Changed a thing.

### Fixed

## [1.29.0] - 2026-06-17

### Added

- Previous release.
"""

    updated = finalize_changelog_release(
        changelog,
        version="1.30.0",
        release_date=datetime.date(2026, 6, 18),
    )

    assert updated.startswith(
        """# Changelog

## [Unreleased]

### Added

### Changed

### Deprecated

### Breaking Changes

### Fixed

### Security

## [1.30.0] - 2026-06-18

### Changed

- Changed a thing.
"""
    )
    assert "### Added\n\n### Changed\n\n- Changed a thing." not in updated


def test_replace_versions() -> None:
    assert (
        replace_project_version(
            '[project]\nname = "temporalio"\nversion = "1.29.0"\n', "1.30.0"
        )
        == '[project]\nname = "temporalio"\nversion = "1.30.0"'
    )
    assert (
        replace_service_version('__version__ = "1.29.0"\n', "1.30.0")
        == '__version__ = "1.30.0"'
    )
    assert (
        replace_service_version(
            '__version__ = "1.29.0"\n\nServiceRequest = TypeVar("ServiceRequest")\n',
            "1.30.0",
        )
        == '__version__ = "1.30.0"\n\nServiceRequest = TypeVar("ServiceRequest")'
    )


def test_create_release_branch_fetches_main_and_branches_from_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], pathlib.Path, bool]] = []

    def run(command: list[str], *, cwd: pathlib.Path, check: bool) -> None:
        calls.append((command, cwd, check))

    monkeypatch.setattr(subprocess, "run", run)

    repo_root = pathlib.Path("/repo")
    create_release_branch(repo_root, "1.30.0")

    assert calls == [
        (["git", "fetch", "origin", "main"], repo_root, True),
        (
            ["git", "switch", "--create", "chore/release-1.30.0", "origin/main"],
            repo_root,
            True,
        ),
    ]


def test_ensure_clean_worktree_rejects_existing_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=" M temporalio/service.py\n"
        ),
    )

    with pytest.raises(RuntimeError, match="clean worktree"):
        ensure_clean_worktree(pathlib.Path("/repo"))


def test_ensure_only_release_changes_rejects_unexpected_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=" M unrelated.txt\n"
        ),
    )

    with pytest.raises(RuntimeError, match="unexpected files: unrelated.txt"):
        ensure_only_release_changes(pathlib.Path("/repo"))


def test_create_release_pr_uses_versioned_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], pathlib.Path, bool]] = []

    def run(command: list[str], *, cwd: pathlib.Path, check: bool) -> None:
        calls.append((command, cwd, check))

    monkeypatch.setattr(subprocess, "run", run)

    repo_root = pathlib.Path("/repo")
    create_release_pr(repo_root, "1.30.0")

    assert calls == [
        (
            [
                "gh",
                "pr",
                "create",
                "--base",
                "main",
                "--head",
                "chore/release-1.30.0",
                "--title",
                "Prepare release 1.30.0",
                "--body",
                "Prepare release 1.30.0.",
            ],
            repo_root,
            True,
        )
    ]


def test_push_release_branch_uses_versioned_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], pathlib.Path, bool]] = []

    def run(command: list[str], *, cwd: pathlib.Path, check: bool) -> None:
        calls.append((command, cwd, check))

    monkeypatch.setattr(subprocess, "run", run)

    repo_root = pathlib.Path("/repo")
    push_release_branch(repo_root, "1.30.0")

    assert calls == [
        (
            ["git", "push", "--set-upstream", "origin", "chore/release-1.30.0"],
            repo_root,
            True,
        )
    ]
