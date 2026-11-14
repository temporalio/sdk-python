"""Helpers that sit on top of the Streams API.

:py:class:`BufferedAppender` batches the appends of one Stream. :py:func:`pipeline` chains
steps over a Stream and runs the whole chain as one ``take``, ``filter`` or ``reduce`` call.
Both take a client Stream or a Workflow Stream.

.. warning::
    This package is experimental and may change in future versions.
"""

from temporalio.contrib.streams._appender import BufferedAppender
from temporalio.contrib.streams._pipeline import Pipeline, pipeline

__all__ = [
    "BufferedAppender",
    "Pipeline",
    "pipeline",
]
