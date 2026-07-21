"""Release workflow validation helpers."""

from __future__ import annotations

import argparse
import ast
import pathlib
import re
import subprocess
from collections.abc import Sequence

try:
    import tomllib
except ModuleNotFoundError:
    import toml as tomllib  # type: ignore[no-redef]


def _checked_in_version() -> str:
    pyproject_version = tomllib.loads(pathlib.Path("pyproject.toml").read_text())[
        "project"
    ]["version"]
    service_tree = ast.parse(pathlib.Path("temporalio/service.py").read_text())
    service_version = None
    for stmt in service_tree.body:
        if (
            isinstance(stmt, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in stmt.targets
            )
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            service_version = stmt.value.value
            break

    if pyproject_version != service_version:
        raise RuntimeError(
            f"pyproject.toml version {pyproject_version!r} does not match "
            f"temporalio/service.py version {service_version!r}"
        )
    if pyproject_version.startswith("v"):
        raise RuntimeError("Checked-in version must not start with 'v'")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+(?:[a-zA-Z0-9_.+-]+)?", pyproject_version):
        raise RuntimeError(f"Invalid checked-in version: {pyproject_version!r}")
    return pyproject_version


def _write_github_output(path: pathlib.Path, *, version: str, sha: str) -> None:
    with path.open("a", encoding="utf-8") as output:
        print(f"version={version}", file=output)
        print(f"sha={sha}", file=output)


def validate_version(args: argparse.Namespace) -> None:
    version = _checked_in_version()
    if args.github_output:
        _write_github_output(
            pathlib.Path(args.github_output),
            version=version,
            sha=args.sha,
        )
    else:
        print(version)


def verify_dist(args: argparse.Namespace) -> None:
    dist_dir = pathlib.Path(args.dist_dir)
    files = sorted(path.name for path in dist_dir.iterdir() if path.is_file())
    wheels = [name for name in files if name.endswith(".whl")]
    sdists = [name for name in files if name.endswith(".tar.gz")]

    if len(files) != len(set(files)):
        raise RuntimeError("Duplicate distribution filenames found")
    expected_sdist = f"temporalio-{args.version}.tar.gz"
    if sdists != [expected_sdist]:
        raise RuntimeError(f"Expected only sdist {expected_sdist!r}, found {sdists!r}")
    if len(wheels) != 7:
        raise RuntimeError(
            f"Expected 7 platform wheels, found {len(wheels)}: {wheels!r}"
        )

    for name in files:
        if not name.startswith(f"temporalio-{args.version}"):
            raise RuntimeError(
                f"Distribution filename does not match requested version "
                f"{args.version!r}: {name}"
            )

    expected_platforms = {
        "linux-x86_64": lambda name: "manylinux" in name and "x86_64" in name,
        "linux-aarch64": lambda name: "manylinux" in name and "aarch64" in name,
        "linux-musl-x86_64": lambda name: "musllinux" in name and "x86_64" in name,
        "linux-musl-aarch64": lambda name: "musllinux" in name and "aarch64" in name,
        "macos-x86_64": lambda name: "macosx" in name and "x86_64" in name,
        "macos-arm64": lambda name: "macosx" in name and "arm64" in name,
        "windows-amd64": lambda name: "win_amd64" in name,
    }
    missing = [
        platform
        for platform, predicate in expected_platforms.items()
        if not any(predicate(name) for name in wheels)
    ]
    if missing:
        raise RuntimeError(
            f"Missing expected platform wheels: {missing!r}; found {wheels!r}"
        )

    print("Verified release artifacts:")
    for name in files:
        print(f"  {name}")


def _git(args: Sequence[str], *, cwd: pathlib.Path | None = None) -> str:
    return subprocess.check_output(
        ["git", *args],
        cwd=cwd,
        encoding="utf-8",
        stderr=subprocess.STDOUT,
    ).strip()


def _version_tuple(version: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)+)(?:[a-zA-Z0-9_.+-]+)?", version)
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _previous_release_tag(version: str) -> str:
    current = _version_tuple(version)
    if current is None:
        raise RuntimeError(f"Cannot determine previous release for {version!r}")

    candidates: list[tuple[int, ...]] = []
    for tag in _git(["tag"]).splitlines():
        tag_version = _version_tuple(tag)
        if tag_version is not None and tag_version < current:
            candidates.append(tag_version)
    if not candidates:
        raise RuntimeError(f"Could not find a previous release tag before {version!r}")
    return ".".join(str(part) for part in max(candidates))


def _gitlink(rev: str, path: str) -> str:
    output = _git(["ls-tree", rev, path])
    parts = output.split()
    if len(parts) < 3 or parts[0] != "160000":
        raise RuntimeError(f"Could not find submodule gitlink {path!r} at {rev!r}")
    return parts[2]


def _clean_commit_subject(subject: str) -> str:
    subject = subject.encode("ascii", "ignore").decode("ascii")
    subject = re.sub(r"\s+", " ", subject).strip()
    subject = re.sub(r"^:[a-z0-9_+-]+:\s*", "", subject)
    return subject.replace(" : ", ": ")


def _link_sdk_core_prs(subject: str) -> str:
    return re.sub(
        r"\(#([0-9]+)\)",
        r"([#\1](https://github.com/temporalio/sdk-rust/pull/\1))",
        subject,
    )


def _sdk_core_release_notes(version: str, path: str) -> list[str]:
    previous_tag = _previous_release_tag(version)
    previous_commit = _gitlink(previous_tag, path)
    current_commit = _gitlink("HEAD", path)
    if previous_commit == current_commit:
        return []

    submodule_path = pathlib.Path(path)
    if not (submodule_path / ".git").exists():
        raise RuntimeError(
            f"Submodule {path!r} is not initialized; checkout with submodules"
        )

    log_args = [
        "log",
        "--format=%H%x00%h%x00%s",
        "--reverse",
        f"{previous_commit}..{current_commit}",
    ]
    try:
        log_output = _git(log_args, cwd=submodule_path)
    except subprocess.CalledProcessError:
        _git(["fetch", "--quiet", "origin", "main"], cwd=submodule_path)
        log_output = _git(log_args, cwd=submodule_path)
    if not log_output:
        return []

    lines = ["### SDK Core", ""]
    for line in log_output.splitlines():
        full_hash, short_hash, subject = line.split("\0", 2)
        subject = _link_sdk_core_prs(_clean_commit_subject(subject))
        lines.append(
            f"- [`{short_hash}`](https://github.com/temporalio/sdk-rust/commit/"
            f"{full_hash}) {subject}"
        )
    return lines


def changelog_notes(args: argparse.Namespace) -> None:
    changelog_path = pathlib.Path(args.changelog)
    lines = changelog_path.read_text(encoding="utf-8").splitlines()
    heading = re.compile(r"^## \[(?P<version>[^\]]+)\](?:\s+-\s+.*)?\s*$")

    start = None
    for index, line in enumerate(lines):
        match = heading.match(line)
        if match and match.group("version") == args.version:
            start = index + 1
            break

    if start is None:
        raise RuntimeError(
            f"Could not find changelog section for version {args.version!r}"
        )

    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break

    section_lines = lines[start:end]
    while section_lines and not section_lines[0].strip():
        section_lines.pop(0)
    while section_lines and not section_lines[-1].strip():
        section_lines.pop()

    if not section_lines:
        raise RuntimeError(f"Changelog section for {args.version!r} is empty")

    note_lines = ["## Notable Changes", "", *section_lines]
    sdk_core_notes = _sdk_core_release_notes(args.version, args.sdk_core_path)
    if sdk_core_notes:
        note_lines.extend(["", *sdk_core_notes])

    notes = "\n".join(note_lines) + "\n"
    pathlib.Path(args.output).write_text(notes, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(required=True)

    validate_parser = subparsers.add_parser("validate-version")
    validate_parser.add_argument("--sha", required=True)
    validate_parser.add_argument("--github-output")
    validate_parser.set_defaults(func=validate_version)

    verify_parser = subparsers.add_parser("verify-dist")
    verify_parser.add_argument("--version", required=True)
    verify_parser.add_argument("--dist-dir", default="dist")
    verify_parser.set_defaults(func=verify_dist)

    changelog_parser = subparsers.add_parser("changelog-notes")
    changelog_parser.add_argument("--version", required=True)
    changelog_parser.add_argument("--changelog", default="CHANGELOG.md")
    changelog_parser.add_argument("--output", required=True)
    changelog_parser.add_argument(
        "--sdk-core-path", default="temporalio/bridge/sdk-core"
    )
    changelog_parser.set_defaults(func=changelog_notes)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
