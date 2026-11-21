"""The Producer of the calling code, and a Producer from its stored form."""

from __future__ import annotations

import temporalio.api.stream.v1
import temporalio.converter._payload_converter
import temporalio.nexus.system._support
import temporalio.workflow
from temporalio.nexus.system.stream_service import Producer


def current_producer() -> Producer:
    """Give the Producer that an append or a close of the calling code sends by default.

    An ``append`` or a ``close`` with no ``producer=`` sends this Producer. In an
    Activity or a Local Activity it is the Producer of the current attempt. In Workflow
    code it is the Workflow's Producer. In any other code it is the client Producer of
    this process. It does no I/O.

    On a Store, an append or a close of Workflow code writes as this Producer, with the
    Workflow Task that runs it as its epoch.

    An Activity can keep the result and pass it to a later ``append`` or ``close`` with
    ``producer=``. That call then writes as this Producer with its epoch, also in a
    later attempt. Heartbeat details give it back as its proto message, so pass it
    through :py:func:`as_producer` first.

    .. warning::
        This API is experimental and subject to change.
    """
    if temporalio.workflow.in_workflow():
        return temporalio.nexus.system._support.workflow_producer()
    return temporalio.nexus.system._support.default_producer()


def as_producer(value: object) -> Producer:
    """Give a Producer for a Producer or for its proto message.

    A Producer that went through a data converter with no type hint, such as one in
    ``activity.info().heartbeat_details``, comes back as the proto message
    ``temporalio.api.stream.v1.Producer``. This function turns it back into the
    :py:class:`temporalio.nexus.system.stream_service.Producer` that ``append`` and
    ``close`` take in ``producer=``. Such a Producer comes back as it is.

    Raises:
        TypeError: The value is neither a Producer nor its proto message.

    .. warning::
        This API is experimental and subject to change.
    """
    if isinstance(value, Producer):
        return value
    if isinstance(value, temporalio.api.stream.v1.Producer):
        converter = (
            temporalio.converter._payload_converter._get_transfer_type_converter(
                Producer
            )
        )
        if converter is None:
            raise TypeError("The generated Producer has no proto transfer type")
        return converter.from_transfer_type(value, Producer)
    raise TypeError(
        f"as_producer takes a Producer or its proto message, not {type(value).__name__}"
    )
