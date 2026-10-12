from __future__ import annotations

import pathlib
import subprocess

import pytest
import tomlkit

import scripts.update_core


def write_crate(path: pathlib.Path, name: str, version: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "Cargo.toml").write_text(
        f'[package]\nname = "{name}"\nversion = "{version}"\nedition = "2021"\n'
    )
    (path / "src").mkdir(exist_ok=True)
    (path / "src/lib.rs").write_text("")


def bridge_fixture(tmp_path: pathlib.Path) -> pathlib.Path:
    bridge = tmp_path / "temporalio/bridge"
    write_crate(bridge, "bridge-fixture", "0.1.0")
    write_crate(bridge / "sdk-core/core", "core-fixture", "0.10.0")
    write_crate(bridge / "sdk-core/client", "client-fixture", "1.1.0")
    write_crate(bridge / "other", "other-fixture", "1.2.0")
    (bridge / "Cargo.toml").write_text(
        (bridge / "Cargo.toml").read_text()
        + "\n[dependencies]\n"
        + "# Keep this comment and formatting.\n"
        + 'core = { package = "core-fixture", version = "0.9", path = "sdk-core/core" }\n'
        + 'client = { package = "client-fixture", version = "1.0", path = "sdk-core/client" }\n'
        + 'other = { package = "other-fixture", version = "1.0", path = "other" }\n'
    )
    return bridge


def test_update_repairs_manifest_preserves_format_and_refreshes_real_lockfile(
    tmp_path: pathlib.Path,
) -> None:
    bridge = bridge_fixture(tmp_path)
    manifest = bridge / "Cargo.toml"
    original = manifest.read_text()
    scripts.update_core.update_bridge_dependencies(tmp_path)
    assert manifest.read_text() == original.replace(
        'version = "0.9"', 'version = "0.10.0"'
    ).replace(
        'package = "client-fixture", version = "1.0"',
        'package = "client-fixture", version = "1.1.0"',
    )
    lock_path = bridge / "Cargo.lock"
    lock = tomlkit.parse(lock_path.read_text()).unwrap()
    assert any(
        package["name"] == "core-fixture" and package["version"] == "0.10.0"
        for package in lock["package"]
    )
    before = manifest.read_bytes(), lock_path.read_bytes()
    scripts.update_core.update_bridge_dependencies(tmp_path)
    assert before == (manifest.read_bytes(), lock_path.read_bytes())


def test_failed_core_update_does_not_change_bridge_files(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = bridge_fixture(tmp_path)
    manifest = bridge / "Cargo.toml"
    original = manifest.read_bytes()

    def fail(_root: pathlib.Path, _args: list[str]) -> str:
        raise RuntimeError("Core import failed")

    monkeypatch.setattr(scripts.update_core, "run_tool", fail)
    with pytest.raises(RuntimeError, match="Core import failed"):
        scripts.update_core.update_core(tmp_path, ["--revision", "main"])
    assert manifest.read_bytes() == original
    assert not (bridge / "Cargo.lock").exists()


def test_refresh_failure_leaves_manifest_for_review(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = bridge_fixture(tmp_path)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, "cargo fetch")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        scripts.update_core.update_bridge_dependencies(tmp_path)
    assert 'version = "0.10.0"' in (bridge / "Cargo.toml").read_text()
