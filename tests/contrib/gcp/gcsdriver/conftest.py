"""Fixtures for GCS tests against a pytest-managed local emulator."""

from __future__ import annotations

import hashlib
import io
import os
import platform
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import aiohttp
import pytest
from gcloud.aio.storage import Storage

_EMULATOR_VERSION = "1.56.1"
_EMULATOR_CHECKSUMS = {
    "Darwin_amd64": "bbcbb78e57c4a7ad242bc971b70a57f963c8daa616ae649254562fbd7f2bc54d",
    "Darwin_arm64": "039d7a520869115b9bc1b354f73612d2dad0aa27b22104f111d8c94985007fe1",
    "Linux_amd64": "0f2f9f6884417acd5cc27126a9aa237ccc627372a4aa5008b399641de6db9469",
    "Linux_arm64": "89a4b887a50834ce5844f47f9938a1203a89330e660b2af7851b858fa489fa96",
    "Windows_amd64": "79c1a92b6d666c6fc2a60439f8ebee9e01b918dad90cae6f003ade4a5877a566",
}


def _emulator_binary(config: pytest.Config) -> Path:
    override = os.environ.get("FAKE_GCS_SERVER_PATH") or shutil.which("fake-gcs-server")
    if override:
        return Path(override).resolve(strict=True)
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine, machine)
    target = f"{platform.system()}_{arch}"
    if target not in _EMULATOR_CHECKSUMS:
        pytest.fail(f"No fake GCS binary for {target}; set FAKE_GCS_SERVER_PATH")
    assert config.cache is not None
    cache = config.cache.mkdir("fake-gcs-server") / _EMULATOR_VERSION / target
    cache.mkdir(parents=True, exist_ok=True)
    filename = (
        "fake-gcs-server.exe" if platform.system() == "Windows" else "fake-gcs-server"
    )
    binary = cache / filename
    if binary.is_file():
        return binary
    url = (
        "https://github.com/fsouza/fake-gcs-server/releases/download/"
        f"v{_EMULATOR_VERSION}/fake-gcs-server_{_EMULATOR_VERSION}_{target}.tar.gz"
    )
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != _EMULATOR_CHECKSUMS[target]:
        pytest.fail(f"Checksum mismatch for {url}")
    with tempfile.TemporaryDirectory(dir=cache) as staging:
        staged_binary = Path(staging) / filename
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            with staged_binary.open("wb") as dest:
                source = archive.extractfile(filename)
                assert source is not None
                with source:
                    shutil.copyfileobj(source, dest)
        staged_binary.chmod(0o755)
        try:
            # Workers may finish downloading together; Windows refuses replacement.
            staged_binary.rename(binary)
        except FileExistsError:
            pass
    return binary


@pytest.fixture(scope="session")
def gcs_server_url(
    pytestconfig: pytest.Config, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[str]:
    endpoint = os.environ.get("STORAGE_EMULATOR_HOST")
    if endpoint:
        yield endpoint.rstrip("/")
        return
    binary = _emulator_binary(pytestconfig)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}"
    log_path = tmp_path_factory.mktemp("fake-gcs-server") / "server.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [
                str(binary),
                "-scheme",
                "http",
                "-host",
                "127.0.0.1",
                "-port",
                str(port),
                "-backend",
                "memory",
                "-external-url",
                endpoint,
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 15
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(
                        f"{endpoint}/storage/v1/b?project=temporal-test", timeout=1
                    ):
                        pass
                except urllib.error.URLError:
                    time.sleep(0.05)
                else:
                    yield endpoint
                    return
            pytest.fail(f"Fake GCS server failed to start:\n{log_path.read_text()}")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


@pytest.fixture
def gcs_bucket() -> str:
    return f"temporal-test-{uuid.uuid4()}"


@pytest.fixture
async def gcs_storage(gcs_server_url: str, gcs_bucket: str) -> AsyncIterator[Storage]:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{gcs_server_url}/storage/v1/b",
            params={"project": "temporal-test"},
            json={"name": gcs_bucket},
        ) as response:
            response.raise_for_status()
        async with Storage(api_root=gcs_server_url) as storage:
            try:
                yield storage
            finally:
                objects = await storage.list_objects(gcs_bucket)
                for item in objects.get("items", []):
                    await storage.delete(gcs_bucket, item["name"])
                async with session.delete(
                    f"{gcs_server_url}/storage/v1/b/{gcs_bucket}"
                ) as response:
                    response.raise_for_status()
