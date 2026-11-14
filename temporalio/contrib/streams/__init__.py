"""Helpers that sit on top of the Streams API.

:py:class:`BufferedAppender` batches the appends of one Stream. It takes a client Stream or a
Workflow Stream.

.. warning::
    This package is experimental and may change in future versions.
"""

from temporalio.contrib.streams._appender import BufferedAppender

__all__ = [
    "BufferedAppender",
]
