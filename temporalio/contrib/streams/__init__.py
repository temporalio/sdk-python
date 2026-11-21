"""Helpers on top of the Streams API.

:py:func:`current_producer` gives the Producer that a write of the running
Activity sends by default, and :py:func:`as_producer` gives back a Producer that
code stored.

.. warning::
    This package is experimental and may change in future versions.
"""

from temporalio.contrib.streams._producer import as_producer, current_producer

__all__ = ["as_producer", "current_producer"]
