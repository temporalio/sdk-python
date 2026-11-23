from __future__ import annotations

import argparse
import datetime
import importlib.util
import pathlib
import subprocess
import sys
from types import ModuleType

import pytest

import scripts.changelog
import scripts.prepare_release
from scripts.prepare_release import (
    commit_release_changes,
    create_release_branch,
    create_release_pr,
    ensure_clean_worktree,
    ensure_only_release_changes,
    prepare_release_files,
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


def test_published_notes_use_shared_file_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    release_verify = _release_verify_module()

    def run_tool(_root: pathlib.Path, args: list[str]) -> str:
        assert args == [
            "release-notes",
            "--version",
            "1.35.0",
            "--changelog",
            "CHANGELOG.md",
            "--submodule",
            "core",
            "--output",
            str(output),
        ]
        output.write_text("Shared release notes.\n")
        return ""

    monkeypatch.setattr(release_verify, "run_tool", run_tool)
    output = tmp_path / "notes.md"
    release_verify.changelog_notes(
        argparse.Namespace(
            version="1.35.0",
            changelog="CHANGELOG.md",
            sdk_core_path="core",
            output=str(output),
        )
    )
    assert output.read_text() == "Shared release notes.\n"


def test_shared_notes_failure_preserves_existing_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    release_verify = _release_verify_module()

    def run_tool(_root: pathlib.Path, _args: list[str]) -> str:
        raise RuntimeError("Missing release section")

    monkeypatch.setattr(release_verify, "run_tool", run_tool)
    output = tmp_path / "notes.md"
    output.write_text("Existing notes.\n")
    with pytest.raises(RuntimeError, match="Missing release section"):
        release_verify.changelog_notes(
            argparse.Namespace(
                version="1.35.0",
                changelog="CHANGELOG.md",
                sdk_core_path="core",
                output=str(output),
            )
        )
    assert output.read_text() == "Existing notes.\n"


def test_shared_tool_preserves_failure_diagnostics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    (tmp_path / "temporalio/bridge/sdk-core/.git").mkdir(parents=True)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, ["cargo"], stderr="invalid fragment")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="invalid fragment"):
        scripts.changelog.run_tool(tmp_path, ["check"])


@pytest.fixture
def release_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    for name, text in {
        "CHANGELOG.md": "# Changelog\n\n## [1.34.0] - 2026-09-30\nOld notes.\n",
        "pyproject.toml": 'version = "1.34.0"\n',
        "temporalio/service.py": '__version__ = "1.34.0"\n',
        "uv.lock": "lock\n",
        "changelog/fixed/giggling-teapot.md": "A fix.\n",
    }.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "Initial",
        ],
        cwd=tmp_path,
        check=True,
    )
    return tmp_path


def mock_preparation(monkeypatch: pytest.MonkeyPatch) -> str:
    changelog = "# Changelog\n\n## [1.35.0] - 2026-10-02\n\n### Fixed\n\n- A fix.\n"

    def prepare(repo: pathlib.Path, version: str, _date: datetime.date) -> None:
        assert version in (repo / "pyproject.toml").read_text()
        assert version in (repo / "temporalio/service.py").read_text()
        (repo / "CHANGELOG.md").write_text(changelog)
        (repo / "changelog/fixed/giggling-teapot.md").unlink()

    monkeypatch.setattr(scripts.prepare_release, "prepare_changelog", prepare)
    return changelog


def test_release_commits_consumed_fragments(
    monkeypatch: pytest.MonkeyPatch, release_repo: pathlib.Path
) -> None:
    changelog = mock_preparation(monkeypatch)
    consumed = prepare_release_files(
        release_repo, "1.35.0", datetime.date(2026, 10, 2), skip_lock=True
    )
    assert consumed == ("changelog/fixed/giggling-teapot.md",)
    assert not (release_repo / consumed[0]).exists()
    ensure_only_release_changes(release_repo, consumed)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=release_repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=release_repo,
        check=True,
    )
    commit_release_changes(release_repo, "1.35.0", consumed)
    deleted = subprocess.check_output(
        ["git", "diff", "--name-only", "--diff-filter=D", "HEAD~", "HEAD"],
        cwd=release_repo,
        text=True,
    ).splitlines()
    assert deleted == list(consumed)
    assert (release_repo / "CHANGELOG.md").read_text() == changelog
    assert "1.35.0" in (release_repo / "temporalio/service.py").read_text()


def test_lock_failure_retains_fragments(
    monkeypatch: pytest.MonkeyPatch, release_repo: pathlib.Path
) -> None:
    mock_preparation(monkeypatch)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, ["uv", "lock"])

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        prepare_release_files(release_repo, "1.35.0", datetime.date(2026, 10, 2))
    assert (release_repo / "changelog/fixed/giggling-teapot.md").exists()


def test_invalid_version_files_do_not_write_notes_or_consume_fragments(
    monkeypatch: pytest.MonkeyPatch, release_repo: pathlib.Path
) -> None:
    mock_preparation(monkeypatch)
    original = (release_repo / "CHANGELOG.md").read_text()
    (release_repo / "temporalio/service.py").write_text("missing version\n")
    with pytest.raises(RuntimeError, match="service version"):
        prepare_release_files(
            release_repo, "1.35.0", datetime.date(2026, 10, 2), skip_lock=True
        )
    assert (release_repo / "CHANGELOG.md").read_text() == original
    assert (release_repo / "changelog/fixed/giggling-teapot.md").exists()


def test_unexpected_changes_rejected_before_consuming(
    monkeypatch: pytest.MonkeyPatch, release_repo: pathlib.Path
) -> None:
    mock_preparation(monkeypatch)
    (release_repo / "changelog/fixed/late-llama.md").write_text("Late change.\n")
    with pytest.raises(RuntimeError, match="unexpected files"):
        prepare_release_files(
            release_repo, "1.35.0", datetime.date(2026, 10, 2), skip_lock=True
        )
    assert (release_repo / "changelog/fixed/giggling-teapot.md").exists()
    assert (release_repo / "changelog/fixed/late-llama.md").exists()


def test_lock_update_precedes_changelog_preparation(
    monkeypatch: pytest.MonkeyPatch, release_repo: pathlib.Path
) -> None:
    calls: list[str] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command == ["uv", "lock"]:
            calls.append("lock")
        return subprocess.CompletedProcess(command, 0, "")

    def prepare(*_args: object) -> None:
        assert calls == ["lock"]
        calls.append("changelog")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(scripts.prepare_release, "prepare_changelog", prepare)
    prepare_release_files(release_repo, "1.35.0", datetime.date(2026, 10, 2))
    assert calls == ["lock", "changelog"]


def test_changelog_rejection_retains_notes_and_version_updates(
    monkeypatch: pytest.MonkeyPatch, release_repo: pathlib.Path
) -> None:
    original = (release_repo / "CHANGELOG.md").read_text()

    def reject(*_args: object) -> None:
        raise RuntimeError("invalid fragment")

    monkeypatch.setattr(scripts.prepare_release, "prepare_changelog", reject)
    with pytest.raises(RuntimeError, match="invalid fragment"):
        prepare_release_files(
            release_repo, "1.35.0", datetime.date(2026, 10, 2), skip_lock=True
        )
    assert (release_repo / "CHANGELOG.md").read_text() == original
    assert (release_repo / "changelog/fixed/giggling-teapot.md").exists()
    assert "1.35.0" in (release_repo / "pyproject.toml").read_text()


def test_replace_versions() -> None:
    assert 'version = "1.30.0"' in replace_project_version(
        'version = "1.29.0"\n', "1.30.0"
    )
    assert '__version__ = "1.30.0"' in replace_service_version(
        '__version__ = "1.29.0"\n', "1.30.0"
    )


def test_release_with_core_update_commits_generated_files(
    monkeypatch: pytest.MonkeyPatch, release_repo: pathlib.Path
) -> None:
    mock_preparation(monkeypatch)
    calls: list[str] = []
    core_fragment = release_repo / "changelog/fixed/core-llama.md"
    generated = release_repo / "temporalio/api/generated.py"
    manifest = release_repo / "temporalio/bridge/Cargo.toml"

    def branch(_root: pathlib.Path, _version: str) -> str:
        calls.append("branch")
        return ""

    def update(root: pathlib.Path, args: list[str]) -> None:
        assert root == release_repo
        assert args == ["--latest-tag"]
        assert calls == ["branch"]
        calls.append("core")
        generated.parent.mkdir(parents=True)
        generated.write_text("# Generated\n")
        manifest.parent.mkdir(parents=True)
        manifest.write_text("# Updated manifest\n")
        core_fragment.write_text("Core: A fix.\n")

    def prepare(root: pathlib.Path, _version: str, _date: datetime.date) -> None:
        assert calls == ["branch", "core"]
        calls.append("release")
        (root / "CHANGELOG.md").write_text("Release notes including Core.\n")
        (root / "changelog/fixed/giggling-teapot.md").unlink()
        core_fragment.unlink()

    monkeypatch.setattr(
        scripts.prepare_release,
        "__file__",
        str(release_repo / "scripts/prepare_release.py"),
    )
    monkeypatch.setattr(scripts.prepare_release, "create_release_branch", branch)
    monkeypatch.setattr(scripts.prepare_release, "update_core", update)
    monkeypatch.setattr(scripts.prepare_release, "prepare_changelog", prepare)
    monkeypatch.setattr(
        scripts.prepare_release,
        "push_release_branch",
        lambda *_args: calls.append("push"),
    )
    monkeypatch.setattr(
        scripts.prepare_release, "create_release_pr", lambda *_args: calls.append("pr")
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=release_repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=release_repo,
        check=True,
    )
    scripts.prepare_release.main(["1.35.0", "--update-core", "--skip-lock"])
    assert calls == ["branch", "core", "release", "push", "pr"]
    ensure_clean_worktree(release_repo)
    assert generated.exists()
    assert manifest.exists()
    assert not core_fragment.exists()
    assert (
        subprocess.check_output(
            ["git", "show", "HEAD:temporalio/api/generated.py"],
            cwd=release_repo,
            text=True,
        )
        == "# Generated\n"
    )


def test_core_update_failure_stops_release(
    monkeypatch: pytest.MonkeyPatch, release_repo: pathlib.Path
) -> None:
    monkeypatch.setattr(
        scripts.prepare_release,
        "__file__",
        str(release_repo / "scripts/prepare_release.py"),
    )
    monkeypatch.setattr(
        scripts.prepare_release, "create_release_branch", lambda *_args: None
    )

    def fail(*_args: object) -> None:
        raise RuntimeError("proto generation failed")

    monkeypatch.setattr(scripts.prepare_release, "update_core", fail)
    with pytest.raises(RuntimeError, match="proto generation failed"):
        scripts.prepare_release.main(["1.35.0", "--update-core", "--skip-lock"])
    ensure_clean_worktree(release_repo)
    assert "1.34.0" in (release_repo / "pyproject.toml").read_text()
    assert (release_repo / "changelog/fixed/giggling-teapot.md").exists()


@pytest.fixture
def release_remote(
    release_repo: pathlib.Path, tmp_path_factory: pytest.TempPathFactory
) -> pathlib.Path:
    remote = tmp_path_factory.mktemp("release-remote") / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    for command in [
        ["git", "config", "user.name", "Test"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "branch", "-M", "main"],
        ["git", "remote", "add", "origin", str(remote)],
        ["git", "push", "-u", "origin", "main"],
    ]:
        subprocess.run(command, cwd=release_repo, check=True)
    return remote


@pytest.mark.parametrize("local_exists", [False, True])
@pytest.mark.parametrize("remote_exists", [False, True])
def test_upsert_release_branch(
    release_repo: pathlib.Path,
    release_remote: pathlib.Path,
    local_exists: bool,
    remote_exists: bool,
) -> None:
    branch = "chore/release-1.35.0"
    subprocess.run(["git", "switch", "-c", branch], cwd=release_repo, check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "Previous release"],
        cwd=release_repo,
        check=True,
    )
    previous = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=release_repo, text=True
    ).strip()
    if remote_exists:
        subprocess.run(["git", "push", "origin", branch], cwd=release_repo, check=True)
    subprocess.run(["git", "switch", "main"], cwd=release_repo, check=True)
    if not local_exists:
        subprocess.run(["git", "branch", "-D", branch], cwd=release_repo, check=True)

    expected = create_release_branch(release_repo, "1.35.0")
    assert expected == (previous if remote_exists else "")
    assert subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=release_repo
    ) == subprocess.check_output(["git", "rev-parse", "main"], cwd=release_repo)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "Regenerated release"],
        cwd=release_repo,
        check=True,
    )
    push_release_branch(release_repo, "1.35.0", expected)
    assert subprocess.check_output(
        ["git", "rev-parse", f"refs/heads/{branch}"], cwd=release_remote
    ) == subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=release_repo)


@pytest.mark.parametrize("remote_exists", [False, True])
def test_release_push_rejects_concurrent_remote_change(
    release_repo: pathlib.Path, release_remote: pathlib.Path, remote_exists: bool
) -> None:
    branch = "chore/release-1.35.0"
    if remote_exists:
        subprocess.run(
            ["git", "push", "origin", f"main:refs/heads/{branch}"],
            cwd=release_repo,
            check=True,
        )
    expected = create_release_branch(release_repo, "1.35.0")
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "Concurrent update"],
        cwd=release_repo,
        check=True,
    )
    subprocess.run(["git", "push", "origin", branch], cwd=release_repo, check=True)
    concurrent_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=release_repo
    )
    create_release_branch(release_repo, "1.35.0")
    with pytest.raises(subprocess.CalledProcessError):
        push_release_branch(release_repo, "1.35.0", expected)
    assert (
        subprocess.check_output(
            ["git", "rev-parse", f"refs/heads/{branch}"], cwd=release_remote
        )
        == concurrent_commit
    )


def test_clean_worktree_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, " M file\0"),
    )
    with pytest.raises(RuntimeError, match="clean worktree"):
        ensure_clean_worktree(pathlib.Path("/repo"))
    with pytest.raises(RuntimeError, match="unexpected files"):
        ensure_only_release_changes(pathlib.Path("/repo"))


@pytest.mark.parametrize(
    "existing_url", ["", "https://github.com/example/repo/pull/123\n"]
)
def test_create_release_pr(monkeypatch: pytest.MonkeyPatch, existing_url: str) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, existing_url)

    monkeypatch.setattr(subprocess, "run", run)
    create_release_pr(pathlib.Path("/repo"), "1.30.0")
    assert calls[0][:3] == ["gh", "pr", "list"]
    assert "chore/release-1.30.0" in calls[0]
    if existing_url:
        assert len(calls) == 1
    else:
        assert calls[1][:3] == ["gh", "pr", "create"]
        assert calls[1][-2:] == ["--label", "skip-changelog"]


def test_push_release_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        subprocess, "run", lambda command, **_kwargs: calls.append(command)
    )
    push_release_branch(pathlib.Path("/repo"), "1.30.0")
    assert calls == [
        [
            "git",
            "push",
            "--force-with-lease=refs/heads/chore/release-1.30.0:",
            "--set-upstream",
            "origin",
            "chore/release-1.30.0",
        ]
    ]
