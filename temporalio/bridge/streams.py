"""The Stream runtime that Core uses to call a Stream Store written in Python. (unstable)

Nothing in this module should be considered stable. The API may change.
"""

from __future__ import annotations

from typing import Any

import temporalio.bridge.runtime
import temporalio.bridge.temporal_sdk_bridge
import temporalio.client


class StreamRuntime:
    """The runtime of the ``temporalio-streams`` crate on one Store written in Python."""

    @staticmethod
    def new_python(
        runtime: temporalio.bridge.runtime.Runtime,
        *,
        caller: Any,
        limits: temporalio.client.StreamBatchLimits,
    ) -> StreamRuntime:
        """Build the runtime on a Store written in Python, which Core calls for Workflow code.

        ``caller`` runs each call of the Store on the Worker's event loop. Core refuses a
        batch over ``limits`` before the Store sees it.
        """
        return StreamRuntime(
            temporalio.bridge.temporal_sdk_bridge.new_python_stream_runtime(
                runtime._ref,
                caller,
                limits.max_record_bytes,
                limits.max_batch_bytes,
                limits.max_batch_records,
            )
        )

    def __init__(self, ref: Any) -> None:
        """Wrap the bridge's runtime object. Use ``new_python`` instead."""
        self._ref = ref
