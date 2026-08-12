from __future__ import annotations

import datetime
import importlib.util
import pathlib
import subprocess
from types import ModuleType

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


def _release_verify_module() -> ModuleType:
    path = pathlib.Path(__file__).parents[1] / ".github/scripts/release_verify.py"
    spec = importlib.util.spec_from_file_location("release_verify", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_finalize_changelog_release_seeds_unreleased_and_versions_notes() -> None:
    changelog = """# Changelog

## [Unreleased]

### Added

### Changed

- Changed a thing.

### :boom: Breaking Changes

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

### :boom: Breaking Changes

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


def test_sdk_core_changelog_entries_include_only_new_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_verify = _release_verify_module()
    changelogs = {
        "old:CHANGELOG.md": """# Changelog

## [Unreleased]

### Added

* Existing feature.

### Fixed

* Existing fix.
""",
        "new:CHANGELOG.md": """# Changelog

## [0.5.0]

### Added

* Existing feature.
* New feature.

### Fixed

* Existing fix.
* New fix.
""",
    }

    monkeypatch.setattr(
        release_verify,
        "_git",
        lambda args, *, cwd=None: changelogs[args[1]],
    )

    assert release_verify._sdk_core_changelog_entries(
        "old", "new", pathlib.Path("sdk-core")
    ) == [
        "#### Added",
        "",
        "* New feature.",
        "",
        "#### Fixed",
        "",
        "* New fix.",
    ]


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
