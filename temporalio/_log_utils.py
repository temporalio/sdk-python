"""Internal utilities for Temporal logging.

This module is internal and may change at any time.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, MutableMapping
from typing import Any, Literal

TemporalLogExtraMode = Literal["dict", "flatten", "json"]
"""Mode controlling how Temporal context is added to log record extra.

Values:
    dict: (default) Add context as a nested dictionary under a single key.
        This is the original behavior. Suitable for logging handlers that
        support nested structures.
    flatten: Add each context field as a separate top-level key with a
        namespaced prefix. Values that are not primitives (str/int/float/bool)
        are converted to strings. This mode is recommended for OpenTelemetry
        and other logging pipelines that require flat, scalar attributes.
    json: Add context as a JSON string under a single key. Useful when
        downstream systems expect string values but you want structured data.
"""


def _apply_temporal_context_to_extra(
    extra: MutableMapping[str, Any],
    *,
    key: str,
    prefix: str,
    ctx: Mapping[str, Any],
    mode: TemporalLogExtraMode,
) -> None:
    """Apply temporal context to log record extra based on the configured mode.

    Args:
        extra: The mutable extra dict to update.
        key: The key to use for dict/json modes (e.g., "temporal_workflow").
        prefix: The prefix to use for flatten mode keys (e.g., "temporal.workflow").
        ctx: The context mapping containing temporal fields.
        mode: The mode controlling how context is added.
    """
    if mode == "dict":
        extra[key] = dict(ctx)
    elif mode == "json":
        extra[key] = json.dumps(ctx, separators=(",", ":"), default=str)
    elif mode == "flatten":
        for k, v in ctx.items():
            # Ensure value is a primitive type safe for OTel attributes
            if not isinstance(v, (str, int, float, bool, type(None))):
                v = str(v)
            extra[f"{prefix}.{k}"] = v
    else:
        # Fallback to dict for any unknown mode (shouldn't happen with typing)
        extra[key] = dict(ctx)


def _update_temporal_context_in_extra(
    extra: MutableMapping[str, Any],
    *,
    key: str,
    prefix: str,
    update_ctx: Mapping[str, Any],
    mode: TemporalLogExtraMode,
) -> None:
    """Update existing temporal context in extra with additional fields.

    This is used when adding update info to existing workflow context.

    Args:
        extra: The mutable extra dict to update.
        key: The key used for dict/json modes (e.g., "temporal_workflow").
        prefix: The prefix used for flatten mode keys (e.g., "temporal.workflow").
        update_ctx: Additional context fields to add/update.
        mode: The mode controlling how context is added.
    """
    if mode == "dict":
        extra.setdefault(key, {}).update(update_ctx)
    elif mode == "json":
        # For JSON mode, we need to parse, update, and re-serialize
        existing = extra.get(key)
        if existing is not None:
            try:
                existing_dict = json.loads(existing)
                existing_dict.update(update_ctx)
                extra[key] = json.dumps(
                    existing_dict, separators=(",", ":"), default=str
                )
            except (json.JSONDecodeError, TypeError):
                # If parsing fails, just create a new JSON object with update_ctx
                extra[key] = json.dumps(
                    dict(update_ctx), separators=(",", ":"), default=str
                )
        else:
            extra[key] = json.dumps(
                dict(update_ctx), separators=(",", ":"), default=str
            )
    elif mode == "flatten":
        for k, v in update_ctx.items():
            # Ensure value is a primitive type safe for OTel attributes
            if not isinstance(v, (str, int, float, bool, type(None))):
                v = str(v)
            extra[f"{prefix}.{k}"] = v
    else:
        # Fallback to dict for any unknown mode
        extra.setdefault(key, {}).update(update_ctx)
