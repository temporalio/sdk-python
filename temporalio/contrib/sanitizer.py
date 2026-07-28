from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

import temporalio.api.common.v1 as api_common
from temporalio.converter import PayloadCodec


_DEFAULT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(api[-_]?key|access[-_]?token|auth[-_]?token|secret|jwt|bearer)"),
    re.compile(r"(?i)(ssn|social[-_]?security|credit[-_]?card|cc[-_]?num)"),
)


def _mask_value(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, (int, float, bool)):
        return val
    return "[REDACTED]"


def _sanitize_obj(obj: Any, patterns: Sequence[re.Pattern[str]]) -> Any:
    if isinstance(obj, Mapping):
        return {
            k: _sanitize_obj(_mask_value(v) if any(p.search(str(k)) for p in patterns) else v, patterns)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_sanitize_obj(v, patterns) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_sanitize_obj(v, patterns) for v in obj)
    return obj


class SanitizingPayloadCodec(PayloadCodec):
    """PayloadCodec that redacts sensitive fields by key pattern.

    This codec preserves payload encoding/type metadata and only rewrites the
    payload data if the decoded JSON is a structured object. When no keys match,
    it is effectively a no-op to minimize overhead.
    """

    def __init__(self, *, key_patterns: Sequence[str] | None = None) -> None:
        pats = key_patterns or []
        self._patterns: tuple[re.Pattern[str], ...] = _DEFAULT_PATTERNS + tuple(
            re.compile(p, re.IGNORECASE) for p in pats
        )

    async def encode(self, payloads: Sequence[api_common.Payload]) -> list[api_common.Payload]:
        out: list[api_common.Payload] = []
        for p in payloads:
            # Only operate on json/plain payloads which are common for dict-like values
            if p.metadata.get("encoding") == b"json/plain":
                try:
                    import json

                    val = json.loads(p.data)
                    sanitized = _sanitize_obj(val, self._patterns)
                    if sanitized is val:
                        out.append(p)
                        continue
                    new = api_common.Payload()
                    new.metadata.update(p.metadata)
                    new.data = json.dumps(sanitized, separators=(",", ":")).encode("utf-8")
                    out.append(new)
                    continue
                except Exception:
                    # On any failure, pass-through to avoid data loss
                    out.append(p)
                    continue
            out.append(p)
        return out

    async def decode(self, payloads: Sequence[api_common.Payload]) -> list[api_common.Payload]:
        # Codec is one-way (sanitizes on encode); decoding is pass-through
        return list(payloads)
