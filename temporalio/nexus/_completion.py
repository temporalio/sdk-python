from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Generic, TypeVar

import nexusrpc

_ResultT = TypeVar("_ResultT")


@dataclass(frozen=True, kw_only=True)
class Completion(Generic[_ResultT]):
    """The result of a completed operation, delivered to a worker for processing.

    This is the base class for all completion variants. Handlers declared with
    :py:func:`temporalio.nexus.completion_operation` should annotate their completion
    parameter with this class so that they continue to work as new completion variants
    are introduced; use ``isinstance`` checks to access variant-specific fields such as
    those of :py:class:`NexusOperationCompletion`.

    Exactly one of :py:attr:`result` and :py:attr:`failure` is set.

    .. warning::
       This API is experimental and unstable.
    """

    result: _ResultT | None = None
    """The result the operation produced if it completed successfully."""

    failure: BaseException | None = None
    """The failure the operation produced if it completed unsuccessfully."""

    links: Sequence[nexusrpc.Link] = field(default_factory=list)
    """Links that should be used to associate any further invocations with the completed operation."""


@dataclass(frozen=True, kw_only=True)
class NexusOperationCompletion(Completion[_ResultT]):
    """The result of a completed Nexus operation, delivered to a worker for processing.

    .. warning::
       This API is experimental and unstable.
    """

    service: str
    """The Nexus service that the result came from."""

    operation: str
    """The Nexus operation within the service that the result came from."""

    operation_id: str
    """The ID of the Nexus operation."""

    operation_token: str
    """The token the operation produced if it was an asynchronous Nexus operation."""
