"""The Producer of a Stream write.

.. warning::
    This module is experimental and subject to change.
"""

from __future__ import annotations

import temporalio.activity
import temporalio.api.stream.v1
import temporalio.converter
from temporalio.nexus.system import _support
from temporalio.nexus.system.stream_service import Producer


def current_producer() -> Producer:
    """Return the Producer of the running attempt of an Activity.

    An append of that attempt sends it by default. Code that stores
    it, such as in heartbeat details, gets it back from :py:func:`as_producer`.

    The Producer of an Activity of a Workflow has the Activity id and the id of
    its ``ActivityTaskScheduled`` event. So a later Activity of the run that uses
    the same Activity id is a separate Producer. A Local Activity whose retry
    waits longer than its ``local_retry_threshold`` runs under a new sequence
    number, and then writes as a new Producer unless the Workflow set
    ``activity_id``.

    Raises:
        RuntimeError: The caller isn't an Activity.

    .. warning::
        This API is experimental and subject to change.
    """
    return _support.activity_producer(temporalio.activity.info())


def as_producer(value: object) -> Producer:
    """Return the Producer that code stored, such as in heartbeat details.

    The default data converter gives a stored Producer back as its proto
    message, since such a value has no type hint.

    Raises:
        TypeError: ``value`` is neither a Producer nor its proto message.

    .. warning::
        This API is experimental and subject to change.
    """
    if isinstance(value, Producer):
        return value
    if not isinstance(value, temporalio.api.stream.v1.Producer):
        raise TypeError(f"Expected a Producer, got {type(value).__name__}")
    converter = temporalio.converter.DataConverter.default
    return _support.model_from_proto(
        value, Producer, converter.payload_converter, converter.failure_converter
    )
