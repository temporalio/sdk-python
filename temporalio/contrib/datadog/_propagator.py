"""Datadog propagation wrapper for Temporal headers."""

from typing import Any, cast

import temporalio.api.common.v1
import temporalio.converter
from temporalio.contrib.datadog._constants import (
    BAGGAGE_ITEM_SERVICE,
    Carrier,
    StringHeader,
    TemporalHeader,
)


class _Propagator:  # type: ignore[reportUnusedClass]
    """Wraps HTTPPropagator with Temporal header encode/decode logic.

    The ``ddtrace`` import is deferred to ``__init__`` so sandbox re-importing
    this module does not import ``ddtrace``.
    """

    def __init__(
        self,
        *,
        header_key: str,
        service_name: str | None,
        payload_converter: temporalio.converter.PayloadConverter,
        allow_invalid_parent_spans: bool = False,
    ) -> None:
        from ddtrace.propagation.http import HTTPPropagator

        self._propagator = HTTPPropagator
        self.header_key = header_key
        self.service_name = service_name
        self._payload_converter = payload_converter
        self.allow_invalid_parent_spans = allow_invalid_parent_spans

    @staticmethod
    def get_baggage(ctx: Any) -> str | None:
        if ctx is None:
            return None

        getter = getattr(ctx, "get_baggage_item", None)
        if callable(getter):
            return cast(str | None, getter(BAGGAGE_ITEM_SERVICE))

        return None

    def set_baggage(self, ctx: Any) -> None:
        if self.service_name is None:
            return
        setter = getattr(ctx, "set_baggage_item", None)
        if callable(setter):
            setter(BAGGAGE_ITEM_SERVICE, self.service_name)

    def inject(self, context: Any) -> Carrier:
        carrier: Carrier = {}
        if context is None:
            return carrier
        self._propagator.inject(context, carrier)
        return carrier

    def extract(self, header: StringHeader | None) -> Any:
        if header is None:
            return None

        try:
            ctx = self._propagator.extract(header)
        except Exception:
            if self.allow_invalid_parent_spans:
                return None
            raise

        if ctx is None or getattr(ctx, "trace_id", None) is None:
            return None

        return ctx

    def _carrier_to_payload(self, carrier: Carrier) -> temporalio.api.common.v1.Payload:
        return self._payload_converter.to_payloads([carrier])[0]

    def _payload_to_carrier(
        self, payload: temporalio.api.common.v1.Payload
    ) -> Carrier | None:
        decoded = self._payload_converter.from_payloads([payload])[0]
        if not isinstance(decoded, dict):
            return None
        return {str(k): str(v) for k, v in decoded.items()}

    def inject_headers(
        self,
        headers: TemporalHeader,
        context: Any,
    ) -> TemporalHeader:
        if context is None:
            return headers

        self.set_baggage(context)
        carrier = self.inject(context)

        return {**headers, self.header_key: self._carrier_to_payload(carrier)}

    def extract_headers(self, headers: TemporalHeader) -> Any:
        payload = headers.get(self.header_key)
        if payload is None:
            return None
        try:
            carrier = self._payload_to_carrier(payload)
        except Exception:
            if self.allow_invalid_parent_spans:
                return None
            raise
        return self.extract(carrier)
