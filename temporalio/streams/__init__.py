"""Native streams — server-side workflow streams.

This module is the SDK surface for the native-streams chasm library
(``temporal/chasm/lib/stream/`` in temporal-server).  It replaces the
contrib :py:mod:`temporalio.contrib.workflow_streams` module's
SDK-layer signal+update implementation with a direct gRPC interface to
the server-side stream primitive.

Status: in development.  The transport layer is currently a
:py:class:`FakeTransport` for testing; production
gRPC transport is a follow-up that depends on Rust-bridge proto
generation.

See ``native-streams/design-overview.html`` for the design.
"""

from ._client import (
    StreamClient,
    compute_payload_hash,
    new_publisher_id,
)
from ._stream import Publisher, Stream
from ._transport import (
    FakeTransport,
    OffsetTruncated,
    PublishAbortedByClose,
    PublishAbortedByOwnerChange,
    PublishGroupAborted,
    SeqRegression,
    StreamAlreadyExists,
    StreamClosed,
    StreamNotFound,
    StreamServiceError,
    Transport,
    TruncateBeyondBase,
    TruncateBeyondHead,
    TruncateBlockedBySubscriber,
)
from ._types import (
    CreateStreamOptions,
    ListStreamsPage,
    PublishItem,
    PublishResult,
    StreamCloseReason,
    StreamDescription,
    StreamState,
    StreamSummary,
    TruncateResult,
)

__all__ = [
    "CreateStreamOptions",
    "FakeTransport",
    "ListStreamsPage",
    "OffsetTruncated",
    "PublishAbortedByClose",
    "PublishAbortedByOwnerChange",
    "PublishGroupAborted",
    "PublishItem",
    "PublishResult",
    "Publisher",
    "SeqRegression",
    "Stream",
    "StreamAlreadyExists",
    "StreamClient",
    "StreamCloseReason",
    "StreamClosed",
    "StreamDescription",
    "StreamNotFound",
    "StreamServiceError",
    "StreamState",
    "StreamSummary",
    "Transport",
    "TruncateBeyondBase",
    "TruncateBeyondHead",
    "TruncateBlockedBySubscriber",
    "TruncateResult",
    "compute_payload_hash",
    "new_publisher_id",
]
