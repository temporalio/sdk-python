"""Tests for the Workspace class."""

import json
from pathlib import Path

import pytest

from temporalio.contrib.workdir import Workspace


@pytest.fixture
def memory_url() -> str:
    """Return a unique memory:// URL for each test."""
    import uuid

    return f"memory://workdir-test/{uuid.uuid4()}"


class TestWorkspace:
    """Tests for Workspace pull/push lifecycle."""

    async def test_empty_remote_starts_empty_local(self, memory_url: str) -> None:
        """First run with no remote archive creates an empty local dir."""
        async with Workspace(memory_url) as ws:
            assert ws.path.exists()
            assert list(ws.path.iterdir()) == []

    async def test_roundtrip_single_file(self, memory_url: str) -> None:
        """Write a file, push, then pull into a new workspace."""
        # Write
        async with Workspace(memory_url) as ws:
            (ws.path / "hello.txt").write_text("world")

        # Read back
        async with Workspace(memory_url) as ws:
            assert (ws.path / "hello.txt").read_text() == "world"

    async def test_roundtrip_nested_directories(self, memory_url: str) -> None:
        """Nested directory structures survive the archive round-trip."""
        async with Workspace(memory_url) as ws:
            (ws.path / "a" / "b").mkdir(parents=True)
            (ws.path / "a" / "b" / "deep.json").write_text('{"nested": true}')
            (ws.path / "top.txt").write_text("top")

        async with Workspace(memory_url) as ws:
            assert json.loads((ws.path / "a" / "b" / "deep.json").read_text()) == {
                "nested": True
            }
            assert (ws.path / "top.txt").read_text() == "top"

    async def test_overwrite_replaces_previous_state(self, memory_url: str) -> None:
        """Second push replaces the first — no stale files from run 1."""
        # Run 1: write file_a
        async with Workspace(memory_url) as ws:
            (ws.path / "file_a.txt").write_text("a")

        # Run 2: write file_b only
        async with Workspace(memory_url) as ws:
            # file_a was pulled from run 1
            assert (ws.path / "file_a.txt").exists()
            # Delete file_a, write file_b
            (ws.path / "file_a.txt").unlink()
            (ws.path / "file_b.txt").write_text("b")

        # Run 3: only file_b should exist
        async with Workspace(memory_url) as ws:
            assert not (ws.path / "file_a.txt").exists()
            assert (ws.path / "file_b.txt").read_text() == "b"

    async def test_exception_skips_push(self, memory_url: str) -> None:
        """If the activity raises, remote state is not updated."""
        # Write initial state
        async with Workspace(memory_url) as ws:
            (ws.path / "original.txt").write_text("safe")

        # Fail mid-activity — should not push
        with pytest.raises(RuntimeError, match="boom"):
            async with Workspace(memory_url) as ws:
                (ws.path / "original.txt").write_text("corrupted")
                (ws.path / "new_file.txt").write_text("should not persist")
                raise RuntimeError("boom")

        # Original state preserved
        async with Workspace(memory_url) as ws:
            assert (ws.path / "original.txt").read_text() == "safe"
            assert not (ws.path / "new_file.txt").exists()

    async def test_cleanup_auto_removes_tempdir(self, memory_url: str) -> None:
        """Auto cleanup removes the temp directory after exit."""
        async with Workspace(memory_url, cleanup="auto") as ws:
            tmpdir = ws.path
            (ws.path / "file.txt").write_text("data")

        assert not tmpdir.exists()

    async def test_cleanup_keep_preserves_dir(self, memory_url: str) -> None:
        """Keep cleanup leaves the local directory in place."""
        async with Workspace(memory_url, cleanup="keep") as ws:
            tmpdir = ws.path
            (ws.path / "file.txt").write_text("data")

        assert tmpdir.exists()
        assert (tmpdir / "file.txt").read_text() == "data"
        # Manual cleanup
        import shutil

        shutil.rmtree(tmpdir)

    async def test_explicit_local_path(
        self, memory_url: str, tmp_path: Path
    ) -> None:
        """User-specified local_path is used instead of a temp directory."""
        local = tmp_path / "my_workspace"
        async with Workspace(memory_url, local_path=local) as ws:
            assert ws.path == local
            (ws.path / "data.txt").write_text("hello")

        # With explicit local_path + auto cleanup, dir should still exist
        # (we only auto-clean tempdirs we created)
        assert local.exists()

    async def test_empty_push_removes_remote_archive(self, memory_url: str) -> None:
        """Pushing an empty directory removes the remote archive."""
        import fsspec

        fs = fsspec.filesystem("memory")
        archive_path = memory_url.replace("memory://", "") + ".tar.gz"

        # Create initial state
        async with Workspace(memory_url) as ws:
            (ws.path / "data.txt").write_text("hello")

        assert fs.exists(archive_path)

        # Push empty
        async with Workspace(memory_url) as ws:
            (ws.path / "data.txt").unlink()

        assert not fs.exists(archive_path)

    async def test_binary_files(self, memory_url: str) -> None:
        """Binary files survive the archive round-trip."""
        binary_data = bytes(range(256))

        async with Workspace(memory_url) as ws:
            (ws.path / "data.bin").write_bytes(binary_data)

        async with Workspace(memory_url) as ws:
            assert (ws.path / "data.bin").read_bytes() == binary_data


class TestWorkspaceExplicitPullPush:
    """Tests for using pull/push directly without context manager."""

    async def test_manual_pull_push(self, memory_url: str) -> None:
        """Pull and push can be called explicitly."""
        ws = Workspace(memory_url)
        await ws.pull()
        (ws.path / "manual.txt").write_text("works")
        await ws.push()

        ws2 = Workspace(memory_url)
        await ws2.pull()
        assert (ws2.path / "manual.txt").read_text() == "works"
