"""Span annotation logic for the Datadog tracing interceptor."""

from collections.abc import Mapping
from typing import Any

from temporalio.contrib.datadog._constants import (
    _MANUAL_KEEP_OPS,
    _MANUAL_KEEP_TAG,
    TEMPORAL_TAG_PREFIX,
    OperationNames,
)


class _SpanAnnotator:  # type: ignore[reportUnusedClass]
    _PEER_SERVICE_TAG = "peer.service"
    _SPAN_KIND_TAG = "span.kind"
    _PRODUCER = "producer"
    _CONSUMER = "consumer"

    _SPAN_KIND: dict[str, str] = {
        OperationNames.START_ACTIVITY: _PRODUCER,
        OperationNames.RUN_ACTIVITY: _CONSUMER,
        OperationNames.START_CHILD_WORKFLOW: _PRODUCER,
        OperationNames.START_WORKFLOW: _PRODUCER,
        OperationNames.SIGNAL_WITH_START_WORKFLOW: _PRODUCER,
        OperationNames.RUN_WORKFLOW: _CONSUMER,
        OperationNames.SIGNAL_WORKFLOW: _PRODUCER,
        OperationNames.SIGNAL_CHILD_WORKFLOW: _PRODUCER,
        OperationNames.SIGNAL_EXTERNAL_WORKFLOW: _PRODUCER,
        OperationNames.HANDLE_SIGNAL: _CONSUMER,
        OperationNames.QUERY_WORKFLOW: _PRODUCER,
        OperationNames.HANDLE_QUERY: _CONSUMER,
        OperationNames.UPDATE_WORKFLOW: _PRODUCER,
        OperationNames.UPDATE_WITH_START_WORKFLOW: _PRODUCER,
        OperationNames.VALIDATE_UPDATE: _CONSUMER,
        OperationNames.HANDLE_UPDATE: _CONSUMER,
        OperationNames.CREATE_SCHEDULE: _PRODUCER,
        OperationNames.START_NEXUS_OPERATION: _PRODUCER,
        OperationNames.RUN_NEXUS_OPERATION_START_HANDLER: _CONSUMER,
        OperationNames.RUN_NEXUS_OPERATION_CANCEL_HANDLER: _CONSUMER,
    }

    def __init__(
        self,
        *,
        service_name: str | None = None,
        extra_tags: Mapping[str, str] | None = None,
    ) -> None:
        self.service_name = service_name
        self.extra_tags: Mapping[str, str] = extra_tags or {}

    @classmethod
    def _normalize_key(cls, key: str) -> str:
        if key.startswith(TEMPORAL_TAG_PREFIX):
            return key
        if key.lower().startswith("temporal"):
            return TEMPORAL_TAG_PREFIX + key[len("temporal") :].lstrip(".")
        return TEMPORAL_TAG_PREFIX + key

    def annotate(
        self,
        span: Any,
        operation: str,
        attributes: Mapping[str, Any] | None,
        parent_service_name: str | None,
        force_keep: bool = False,
    ) -> None:
        # User-defined global custom tags
        for key, value in self.extra_tags.items():
            span.set_tag(key, value)

        # Attributes from the operation
        if attributes:
            for key, value in attributes.items():
                span.set_tag(self._normalize_key(key), value)

        # Force-keep entry-point operations that have no local parent.
        # Two parent shapes qualify: no parent (nil/None — scheduled or standalone
        # execution) and parents extracted from Temporal task headers (cross-process).
        # Parents from context_provider.active() are in-process producer spans and
        # should inherit the caller's sampling decision instead.
        if operation in _MANUAL_KEEP_OPS and force_keep:
            span.set_tag(_MANUAL_KEEP_TAG, True)

        kind = self._SPAN_KIND.get(operation)
        if kind:
            span.set_tag(self._SPAN_KIND_TAG, kind)
            if (
                kind == self._CONSUMER
                and parent_service_name
                and parent_service_name != self.service_name
            ):
                span.set_tag(self._PEER_SERVICE_TAG, parent_service_name)
