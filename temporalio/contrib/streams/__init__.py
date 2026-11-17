"""Helpers that sit on top of the Streams API.

:py:class:`BufferedAppender` batches the appends of one Stream. It takes a client Stream or a
Workflow Stream. :py:func:`current_producer` gives the Producer that an append of the
calling code sends by default.

.. warning::
    This package is experimental and may change in future versions.
"""

from temporalio.contrib.streams._appender import BufferedAppender
from temporalio.contrib.streams._producer import current_producer

__all__ = [
    "BufferedAppender",
    "current_producer",
]
