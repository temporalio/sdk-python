"""Prepare checked-in files for an SDK release."""

from __future__ import annotations

import argparse
import datetime
import pathlib
import re
import subprocess
import sys
from collections.abc import Sequence

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts.changelog import prepare_changelog

VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+)+(?:[a-zA-Z0-9_.+-]+)?")
_RELEASE_FILES = (
    "CHANGELOG.md",
    "pyproject.toml",
    "temporalio/service.py",
    "uv.lock",
)


def validate_version(version: str) -> str:
    if not VERSION_RE.fullmatch(version):
        raise ValueError(
            f"Invalid version {version!r}; expected a version like '1.30.0'"
        )
    return version


def parse_date(date: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(date)
    except ValueError as err:
        raise ValueError(f"Invalid release date {date!r}; expected YYYY-MM-DD") from err


def replace_project_version(text: str, version: str) -> str:
    return _replace_once(
        r'(?m)^version = "[^"]+"[^\S\r\n]*$',
        f'version = "{validate_version(version)}"',
        text,
        description="project version",
    )


def replace_service_version(text: str, version: str) -> str:
    return _replace_once(
        r'(?m)^__version__ = "[^"]+"[^\S\r\n]*$',
        f'__version__ = "{validate_version(version)}"',
        text,
        description="service version",
    )


def create_release_branch(repo_root: pathlib.Path, version: str) -> None:
    subprocess.run(["git", "fetch", "origin", "main"], cwd=repo_root, check=True)
    subprocess.run(
        ["git", "switch", "--create", f"chore/release-{version}", "origin/main"],
        cwd=repo_root,
        check=True,
    )
    subprocess.run(["git", "submodule", "update", "--init"], cwd=repo_root, check=True)


def changed_files(repo_root: pathlib.Path) -> set[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain", "-z", "--untracked-files=all"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return {entry[3:] for entry in result.stdout.split("\0") if entry}


def ensure_clean_worktree(repo_root: pathlib.Path) -> None:
    changes = changed_files(repo_root)
    if changes:
        raise RuntimeError(
            "Release preparation requires a clean worktree; found changes in "
            + ", ".join(sorted(changes))
        )


def ensure_only_release_changes(
    repo_root: pathlib.Path, consumed_paths: Sequence[str] = ()
) -> None:
    unexpected_files = (
        changed_files(repo_root) - set(_RELEASE_FILES) - set(consumed_paths)
    )
    if unexpected_files:
        raise RuntimeError(
            "Release preparation changed unexpected files: "
            + ", ".join(sorted(unexpected_files))
        )


def commit_release_changes(
    repo_root: pathlib.Path, version: str, consumed_paths: Sequence[str] = ()
) -> None:
    subprocess.run(
        [
            "git",
            "commit",
            "-m",
            f"Prepare release {version}",
            "--",
            *_RELEASE_FILES,
            *consumed_paths,
        ],
        cwd=repo_root,
        check=True,
    )


def push_release_branch(repo_root: pathlib.Path, version: str) -> None:
    branch = f"chore/release-{version}"
    subprocess.run(
        ["git", "push", "--set-upstream", "origin", branch],
        cwd=repo_root,
        check=True,
    )


def create_release_pr(repo_root: pathlib.Path, version: str) -> None:
    branch = f"chore/release-{version}"
    subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--base",
            "main",
            "--head",
            branch,
            "--title",
            f"Prepare release {version}",
            "--body",
            f"Prepare release {version}.",
            "--label",
            "skip-changelog",
        ],
        cwd=repo_root,
        check=True,
    )


def _replace_once(
    pattern: str,
    replacement: str,
    text: str,
    *,
    description: str,
) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1)
    if count != 1:
        raise RuntimeError(f"Could not find {description}")
    return updated.rstrip("\n")


def prepare_release_files(
    repo_root: pathlib.Path,
    version: str,
    release_date: datetime.date,
    *,
    skip_lock: bool = False,
) -> tuple[str, ...]:
    """Update Python versions and lockfile before preparing the shared changelog."""
    pyproject_path = repo_root / "pyproject.toml"
    service_path = repo_root / "temporalio/service.py"
    pyproject_text = (
        replace_project_version(pyproject_path.read_text(encoding="utf-8"), version)
        + "\n"
    )
    service_text = (
        replace_service_version(service_path.read_text(encoding="utf-8"), version)
        + "\n"
    )
    pyproject_path.write_text(pyproject_text, encoding="utf-8")
    service_path.write_text(service_text, encoding="utf-8")
    if not skip_lock:
        subprocess.run(["uv", "lock"], cwd=repo_root, check=True)
    ensure_only_release_changes(repo_root)
    prepare_changelog(repo_root, version, release_date)
    result = subprocess.run(
        ["git", "ls-files", "--deleted", "-z", "--", "changelog"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(path for path in result.stdout.split("\0") if path)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Collect changelog fragments into a dated release, bump versions, "
            "refresh uv.lock, and open a PR."
        )
    )
    parser.add_argument("version", help="Release version, for example 1.30.0")
    parser.add_argument(
        "--date",
        default=datetime.date.today().isoformat(),
        help="Release date in YYYY-MM-DD format. Defaults to today.",
    )
    parser.add_argument(
        "--skip-lock",
        action="store_true",
        help="Do not run 'uv lock'. Intended only for local testing.",
    )
    args = parser.parse_args(argv)

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    version = validate_version(args.version)
    release_date = parse_date(args.date)
    ensure_clean_worktree(repo_root)
    create_release_branch(repo_root, version)
    consumed = prepare_release_files(
        repo_root, version, release_date, skip_lock=args.skip_lock
    )
    ensure_only_release_changes(repo_root, consumed)
    commit_release_changes(repo_root, version, consumed)
    push_release_branch(repo_root, version)
    create_release_pr(repo_root, version)

    print(
        f"Prepared release {version} dated {release_date.isoformat()} and opened a PR"
    )


if __name__ == "__main__":
    main()
