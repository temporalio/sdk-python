"""A Workflow read of a Stream that waits for a predicate.

.. warning::
    This API is an experimental prototype and subject to change.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Generic, TypeVar

from ._context import _Runtime

if TYPE_CHECKING:
    # The generated Stream modules import `temporalio.workflow`, so this module names
    # their types for the type checker only.
    from temporalio.nexus.system.stream_service.models import StreamRecord
    from temporalio.nexus.system.stream_service.workflow._resources.stream import (
        Stream,
    )

__all__ = ["StreamRead", "read_stream"]

BodyT = TypeVar("BodyT")

DEFAULT_READ_STREAM_MAX_RECORDS = 10_000
"""The most Records that one :py:func:`read_stream` gives when ``max_records`` is ``None``."""


@dataclass(frozen=True)
class StreamRead(Generic[BodyT]):
    """What one :py:func:`read_stream` gave.

    .. warning::
        This API is an experimental prototype and subject to change.
    """

    records: Sequence[StreamRecord[BodyT]]
    """The Records of the read in order, typed as a client's read gives them."""

    last_offset: bytes | None
    """The offset of the last Record, or the ``from_offset`` of a read with none.

    Give it as the ``from_offset`` of the next read.
    """

    matched: bool
    """Whether ``until`` was true for the last Record."""

    done: bool
    """Whether the Stream is closed and its last Record is in :py:attr:`records`."""


async def read_stream(
    stream: Stream[BodyT],
    *,
    until: Callable[[StreamRecord[BodyT]], bool] | None = None,
    from_offset: bytes | None = None,
    max_records: int | None = None,
    timeout: timedelta,
) -> StreamRead[BodyT]:
    """Read a Stream until a predicate is true, in one Local Activity.

    The read starts after ``from_offset`` and stops at the first of these. ``until``
    is true for a Record, ``max_records`` Records were read, the Stream is closed and
    its last Record was read, or ``timeout`` passed. A Store's Local Activity reads
    the pages, and History gets one marker for the whole read. The marker has the
    range of offsets and no Record. A replay reads that range from the Store again
    before the Workflow sees the result, so the Records must still be in the Store.

    Args:
        stream: The Workflow ``Stream`` to read, from
            :py:func:`temporalio.workflow.get_stream` or
            :py:func:`temporalio.workflow.create_stream`.
        until: Called with each typed Record in order. The read stops after the first
            Record for which it is true, and Records after it stay for the next read.
            It runs in the Worker process, on the Worker's event loop, while the Local
            Activity reads. It runs outside the Workflow's own thread. So it must not
            change any state, and it can't call a function of ``temporalio.workflow``.
            It can read the Workflow's state through a reference such as ``self``.
            When it sees a change of that state is not defined. A replay does not call
            it, since the replay takes the end of the read from History. ``None``
            reads until another limit stops the read.
        from_offset: Read after this offset. ``None`` is the beginning.
        max_records: The most Records to read. ``None`` is
            :py:data:`DEFAULT_READ_STREAM_MAX_RECORDS`.
        timeout: How long the read waits for a match. It must be positive.

    Returns:
        The Records of the read, the offset to read after next, and why it stopped.

    Raises:
        temporalio.exceptions.StreamError: The Store failed the read.
        temporalio.exceptions.ApplicationError: ``until`` raised. The error has the
            message of the exception.
        temporalio.exceptions.StreamConfigError: The Worker's client has no Stream
            config. This fails the Workflow Task.

    .. warning::
        This API is an experimental prototype and subject to change.
    """
    if timeout <= timedelta():
        raise ValueError("timeout must be positive")
    if max_records is None:
        max_records = DEFAULT_READ_STREAM_MAX_RECORDS
    elif max_records < 1:
        raise ValueError("max_records must be at least 1")
    return await _Runtime.current().workflow_read_stream(
        stream,
        until=until,
        from_offset=from_offset,
        max_records=max_records,
        timeout=timeout,
    )
