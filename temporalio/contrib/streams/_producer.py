"""The Producer of the calling code."""

from __future__ import annotations

import temporalio.nexus.system._support
import temporalio.workflow
from temporalio.nexus.system.stream_service import Producer


def current_producer() -> Producer:
    """Give the Producer that an append or a close of the calling code sends by default.

    An ``append`` or a ``close`` with no ``producer=`` sends this Producer. In an
    Activity or a Local Activity it is the Producer of the current attempt. In Workflow
    code it is the Workflow's Producer. In any other code it is the client Producer of
    this process. It does no I/O.

    On a Store, an append of Workflow code writes as the Local Activity that runs it.
    That holds also when the code passes this Producer in ``producer=``.

    An Activity can keep the result and pass it to a later ``append`` or ``close`` with
    ``producer=``. That call then writes as this Producer with its epoch, also in a
    later attempt.

    .. warning::
        This API is experimental and subject to change.
    """
    if temporalio.workflow.in_workflow():
        return temporalio.nexus.system._support.workflow_producer()
    return temporalio.nexus.system._support.default_producer()
