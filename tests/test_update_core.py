from __future__ import annotations

import pathlib
import subprocess

import pytest
import tomlkit

import scripts.update_core


@pytest.mark.parametrize(
    "tags, expected",
    [
        (
            "core-v0.9.0\ncore-v0.11.0\ncore-v0.10.2\ncore-v0.12.0-rc.1\n"
            "sdk-v99.0.0\ncore-v0.11.1\n",
            "core-v0.11.1",
        ),
        ("core-v1.0.0\ncore-v0.99.0\n", "core-v1.0.0"),
    ],
)
def test_latest_core_tag(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    tags: str,
    expected: str,
) -> None:
    (tmp_path / "temporalio/bridge/sdk-core/.git").mkdir(parents=True)
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["cwd"] == tmp_path / "temporalio/bridge/sdk-core"
        assert kwargs["check"] is True
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, tags)

    monkeypatch.setattr(subprocess, "run", run)
    assert scripts.update_core.latest_core_tag(tmp_path) == expected
    assert calls == [
        ["git", "fetch", "origin", "--tags"],
        ["git", "tag", "--list", "core-v*"],
    ]


def test_latest_core_tag_requires_stable_release(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "temporalio/bridge/sdk-core/.git").mkdir(parents=True)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 0, "core-v1.0.0-rc.1\n"
        ),
    )
    with pytest.raises(RuntimeError, match="No stable"):
        scripts.update_core.latest_core_tag(tmp_path)


def test_latest_core_tag_requires_initialized_submodule(tmp_path: pathlib.Path) -> None:
    with pytest.raises(RuntimeError, match="git submodule update --init"):
        scripts.update_core.latest_core_tag(tmp_path)


@pytest.mark.parametrize(
    "args, expected",
    [
        ([], []),
        (["--latest-tag"], ["--revision", "core-v0.11.0"]),
        (["--revision", "main"], ["--revision", "main"]),
        (["--revision=main"], ["--revision", "main"]),
    ],
)
def test_update_core_orders_import_lock_and_protos(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    expected: list[str],
) -> None:
    calls: list[str] = []

    def latest(_root: pathlib.Path) -> str:
        calls.append("tag")
        return "core-v0.11.0"

    def import_core(_root: pathlib.Path, tool_args: list[str]) -> str:
        assert tool_args == [
            "update-core",
            "--submodule",
            "temporalio/bridge/sdk-core",
            *expected,
        ]
        calls.append("import")
        return ""

    def generate(command: list[str], **kwargs: object) -> None:
        assert command == ["poe", "gen-protos"]
        assert kwargs == {"cwd": tmp_path, "check": True}
        calls.append("protos")

    monkeypatch.setattr(scripts.update_core, "latest_core_tag", latest)
    monkeypatch.setattr(scripts.update_core, "run_tool", import_core)
    monkeypatch.setattr(
        scripts.update_core,
        "update_bridge_dependencies",
        lambda _root: calls.append("lock"),
    )
    monkeypatch.setattr(subprocess, "run", generate)
    scripts.update_core.update_core(tmp_path, args)
    assert calls == (["tag"] if args == ["--latest-tag"] else []) + [
        "import",
        "lock",
        "protos",
    ]


@pytest.mark.parametrize("revision", [["--revision", "main"], ["--revision=main"]])
def test_latest_tag_and_revision_are_mutually_exclusive(
    tmp_path: pathlib.Path, revision: list[str]
) -> None:
    with pytest.raises(SystemExit) as err:
        scripts.update_core.update_core(tmp_path, ["--latest-tag", *revision])
    assert err.value.code == 2


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
