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

CHANGELOG_HEADERS = (
    "Added",
    "Changed",
    "Deprecated",
    "Breaking Changes",
    "Fixed",
    "Security",
)
VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+)+(?:[a-zA-Z0-9_.+-]+)?")
_CHANGELOG_HEADING_RE = re.compile(r"^## \[(?P<version>[^\]]+)\](?:\s+-\s+.*)?\s*$")
_CHANGELOG_SUBHEADING_RE = re.compile(r"^### (?P<header>.+?)\s*$")


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


def finalize_changelog_release(
    text: str,
    *,
    version: str,
    release_date: datetime.date,
) -> str:
    validate_version(version)
    lines = text.splitlines()

    if _find_version_section(lines, version) is not None:
        raise RuntimeError(f"Changelog already has a section for {version!r}")

    unreleased = _find_version_section(lines, "Unreleased")
    if unreleased is None:
        raise RuntimeError("Could not find changelog section for 'Unreleased'")

    heading_index, section_start, section_end = unreleased
    unreleased_lines = _strip_empty_changelog_headers(
        _strip_outer_blank_lines(lines[section_start:section_end])
    )
    if not unreleased_lines:
        raise RuntimeError("Changelog section for 'Unreleased' is empty")

    next_lines = [
        *lines[:heading_index],
        *_seeded_unreleased_lines(),
        f"## [{version}] - {release_date.isoformat()}",
        "",
        *unreleased_lines,
        "",
        *lines[section_end:],
    ]
    return "\n".join(_collapse_blank_lines(next_lines)).rstrip() + "\n"


def replace_project_version(text: str, version: str) -> str:
    return _replace_once(
        r'(?m)^version = "[^"]+"\s*$',
        f'version = "{validate_version(version)}"',
        text,
        description="project version",
    )


def replace_service_version(text: str, version: str) -> str:
    return _replace_once(
        r'(?m)^__version__ = "[^"]+"\s*$',
        f'__version__ = "{validate_version(version)}"',
        text,
        description="service version",
    )


def _seeded_unreleased_lines() -> list[str]:
    lines = ["## [Unreleased]", ""]
    for header in CHANGELOG_HEADERS:
        lines.extend([f"### {header}", ""])
    return lines


def _strip_empty_changelog_headers(lines: list[str]) -> list[str]:
    filtered: list[str] = []
    index = 0
    while index < len(lines):
        match = _CHANGELOG_SUBHEADING_RE.match(lines[index])
        if not match or match.group("header") not in CHANGELOG_HEADERS:
            filtered.append(lines[index])
            index += 1
            continue

        next_index = index + 1
        while next_index < len(lines) and not lines[next_index].startswith("### "):
            next_index += 1

        content = lines[index + 1 : next_index]
        if any(line.strip() for line in content):
            filtered.append(lines[index])
            filtered.extend(content)
        index = next_index

    return _strip_outer_blank_lines(filtered)


def _find_version_section(
    lines: list[str],
    version: str,
) -> tuple[int, int, int] | None:
    for index, line in enumerate(lines):
        match = _CHANGELOG_HEADING_RE.match(line)
        if match and match.group("version") == version:
            section_end = len(lines)
            for end_index in range(index + 1, len(lines)):
                if lines[end_index].startswith("## "):
                    section_end = end_index
                    break
            return index, index + 1, section_end
    return None


def _strip_outer_blank_lines(lines: list[str]) -> list[str]:
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _collapse_blank_lines(lines: list[str]) -> list[str]:
    collapsed: list[str] = []
    previous_blank = False
    for line in lines:
        blank = not line.strip()
        if blank and previous_blank:
            continue
        collapsed.append(line)
        previous_blank = blank
    return collapsed


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


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Bump the SDK version, roll CHANGELOG.md's Unreleased section into "
            "a dated release section, seed a fresh Unreleased section, and "
            "refresh uv.lock."
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
    changelog_path = repo_root / "CHANGELOG.md"
    pyproject_path = repo_root / "pyproject.toml"
    service_path = repo_root / "temporalio" / "service.py"

    changelog_text = finalize_changelog_release(
        changelog_path.read_text(encoding="utf-8"),
        version=version,
        release_date=release_date,
    )
    pyproject_text = (
        replace_project_version(
            pyproject_path.read_text(encoding="utf-8"),
            version,
        )
        + "\n"
    )
    service_text = (
        replace_service_version(
            service_path.read_text(encoding="utf-8"),
            version,
        )
        + "\n"
    )

    changelog_path.write_text(changelog_text, encoding="utf-8")
    pyproject_path.write_text(pyproject_text, encoding="utf-8")
    service_path.write_text(service_text, encoding="utf-8")

    if not args.skip_lock:
        subprocess.run(["uv", "lock"], cwd=repo_root, check=True)

    print(f"Prepared release {version} dated {release_date.isoformat()}")


if __name__ == "__main__":
    main()
