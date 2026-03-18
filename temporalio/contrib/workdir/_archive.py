"""Archive utilities for packing/unpacking workspace directories."""

import io
import tarfile
from pathlib import Path


def pack(directory: Path) -> bytes:
    """Pack a directory into a gzipped tar archive.

    Args:
        directory: Local directory to archive. Must exist.

    Returns:
        The tar.gz archive as bytes.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for entry in sorted(directory.rglob("*")):
            if entry.is_file():
                arcname = str(entry.relative_to(directory))
                tar.add(str(entry), arcname=arcname)
    return buf.getvalue()


def unpack(data: bytes, directory: Path) -> None:
    """Unpack a gzipped tar archive into a directory.

    Args:
        data: The tar.gz archive bytes.
        directory: Target directory. Created if it doesn't exist.
    """
    directory.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO(data)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        # Security: prevent path traversal
        for member in tar.getmembers():
            member_path = Path(directory / member.name).resolve()
            if not str(member_path).startswith(str(directory.resolve())):
                raise ValueError(
                    f"Archive member {member.name!r} would escape target directory"
                )
        tar.extractall(path=str(directory), filter="data")
