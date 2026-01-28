"""Internal utilities for Temporal logging.

This module is internal and may change at any time.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any, Literal

TemporalLogExtraMode = Literal["dict", "flatten"]
"""Mode controlling how Temporal context is added to log record extra.

Values:
    dict: (default) Add context as a nested dictionary under a single key.
        This is the original behavior. Suitable for logging handlers that
        support nested structures.
    flatten: Add each context field as a separate top-level key with a
        namespaced prefix. Values that are not primitives (str/int/float/bool)
        are converted to strings. This mode is recommended for OpenTelemetry
        and other logging pipelines that require flat, scalar attributes.
"""


def _apply_temporal_context_to_extra(
    extra: MutableMapping[str, Any],
    *,
    key: str,
    ctx: Mapping[str, Any],
    mode: TemporalLogExtraMode,
) -> None:
    """Apply temporal context to log record extra based on the configured mode.

    Args:
        extra: The mutable extra dict to update.
        key: The base key (e.g., "temporal_workflow"). In dict mode this is
            used directly. In flatten mode the prefix is derived by replacing
            underscores with dots (e.g., "temporal.workflow").
        ctx: The context mapping containing temporal fields.
        mode: The mode controlling how context is added.
    """
    if mode == "flatten":
        prefix = key.replace("_", ".")
        for k, v in ctx.items():
            # Ensure value is a primitive type safe for OTel attributes
            if not isinstance(v, (str, int, float, bool, type(None))):
                v = str(v)
            extra[f"{prefix}.{k}"] = v
    else:
        extra[key] = dict(ctx)


def _update_temporal_context_in_extra(
    extra: MutableMapping[str, Any],
    *,
    key: str,
    update_ctx: Mapping[str, Any],
    mode: TemporalLogExtraMode,
) -> None:
    """Update existing temporal context in extra with additional fields.

    This is used when adding update info to existing workflow context.

    Args:
        extra: The mutable extra dict to update.
        key: The base key (e.g., "temporal_workflow"). In dict mode this is
            used directly. In flatten mode the prefix is derived by replacing
            underscores with dots (e.g., "temporal.workflow").
        update_ctx: Additional context fields to add/update.
        mode: The mode controlling how context is added.
    """
    if mode == "flatten":
        prefix = key.replace("_", ".")
        for k, v in update_ctx.items():
            # Ensure value is a primitive type safe for OTel attributes
            if not isinstance(v, (str, int, float, bool, type(None))):
                v = str(v)
            extra[f"{prefix}.{k}"] = v
    else:
        extra.setdefault(key, {}).update(update_ctx)
