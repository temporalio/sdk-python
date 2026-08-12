from __future__ import annotations

import datetime
import importlib.util
import pathlib
import subprocess
import sys
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
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_sdk_core_changelog_entries_runs_core_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_verify = _release_verify_module()
    calls: list[tuple[list[str], pathlib.Path]] = []

    def check_output(args: list[str], *, cwd: pathlib.Path, **_kwargs: object) -> str:
        calls.append((args, cwd))
        return "#### Added\n\n* Core feature.\n"

    monkeypatch.setattr(subprocess, "check_output", check_output)
    core_path = pathlib.Path("sdk-core")
    assert release_verify._sdk_core_changelog_entries("old", "new", core_path) == [
        "#### Added",
        "",
        "* Core feature.",
    ]
    assert calls == [
        (
            [
                "python3",
                "scripts/changelog_release_notes.py",
                "--from",
                "old",
                "--to",
                "new",
            ],
            core_path,
        )
    ]


def test_sdk_core_release_notes_embed_core_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    release_verify = _release_verify_module()
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(release_verify, "_previous_release_tag", lambda _version: "old")
    monkeypatch.setattr(release_verify, "_gitlink", lambda revision, _path: revision)
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *_args, **_kwargs: "#### Commits\n\n- Core commit\n",
    )

    assert release_verify._sdk_core_release_notes("1.30.0", str(tmp_path)) == [
        "### SDK Core",
        "",
        "#### Commits",
        "",
        "- Core commit",
    ]


def test_finalize_changelog_release() -> None:
    text = "## [Unreleased]\n\n### Added\n\n- A thing.\n"
    assert "## [1.30.0] - 2026-06-18" in finalize_changelog_release(
        text, version="1.30.0", release_date=datetime.date(2026, 6, 18)
    )


def test_replace_versions() -> None:
    assert 'version = "1.30.0"' in replace_project_version(
        'version = "1.29.0"\n', "1.30.0"
    )
    assert '__version__ = "1.30.0"' in replace_service_version(
        '__version__ = "1.29.0"\n', "1.30.0"
    )


def test_create_release_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        subprocess, "run", lambda command, **_kwargs: calls.append(command)
    )
    create_release_branch(pathlib.Path("/repo"), "1.30.0")
    assert calls[1] == [
        "git",
        "switch",
        "--create",
        "chore/release-1.30.0",
        "origin/main",
    ]


def test_clean_worktree_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, " M file\n"),
    )
    with pytest.raises(RuntimeError, match="clean worktree"):
        ensure_clean_worktree(pathlib.Path("/repo"))
    with pytest.raises(RuntimeError, match="unexpected files"):
        ensure_only_release_changes(pathlib.Path("/repo"))


def test_create_release_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        subprocess, "run", lambda command, **_kwargs: calls.append(command)
    )
    create_release_pr(pathlib.Path("/repo"), "1.30.0")
    assert "chore/release-1.30.0" in calls[0]


def test_push_release_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        subprocess, "run", lambda command, **_kwargs: calls.append(command)
    )
    push_release_branch(pathlib.Path("/repo"), "1.30.0")
    assert calls == [
        ["git", "push", "--set-upstream", "origin", "chore/release-1.30.0"]
    ]
