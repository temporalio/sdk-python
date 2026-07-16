"""Sandbox passthrough-list merging shared by the LangChain-family plugins.

Only the MECHANISM lives here; each plugin owns its module list (the lists
are behavior for released plugins and must not drift by sharing).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence


def merge_passthrough_modules(
    defaults: Sequence[str], user: Iterable[str] | None
) -> tuple[str, ...]:
    """Merge caller-supplied passthrough modules with a plugin's defaults.

    Order-preserving: defaults first, then user additions, first occurrence
    wins on duplicates.
    """
    merged = [*defaults, *(user or ())]
    # dict.fromkeys preserves order while de-duplicating.
    return tuple(dict.fromkeys(merged))
