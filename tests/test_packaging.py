"""Packaging configuration regression tests."""

from pathlib import Path
import tomllib


def test_maturin_excludes_sdk_core_gitfile() -> None:
    """Built wheels must not install the sdk-core submodule's gitfile."""
    project = tomllib.loads(Path("pyproject.toml").read_text())

    assert "temporalio/bridge/sdk-core/.git" in project["tool"]["maturin"]["exclude"]
