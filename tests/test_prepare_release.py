from __future__ import annotations

import datetime

from scripts.prepare_release import (
    finalize_changelog_release,
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
