"""Version-compatibility shims for the ``google-genai`` SDK.

Single home for workarounds that keep the plugin importable and
type-checkable across the supported ``google-genai`` range; remove entries
as upstream fixes land.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # google-genai 2.12.0 exports two ``Interaction`` names: the interaction
    # resource class and a ``TypeAliasType`` over the trigger-request
    # variants. At runtime the resource class deliberately wins the
    # star-import ordering in ``google.genai.interactions`` (see the comment
    # there), but type checkers resolve the name to the alias — which cannot
    # be used with ``isinstance`` and lacks the resource fields (``id``,
    # ``status``, ...). Bind the static view to the class's defining module
    # (the same location in 2.10.x); the runtime binding stays on the public
    # path, so behavior is unchanged.
    from google.genai._gaos.types.interactions.interaction import Interaction
else:
    from google.genai.interactions import Interaction

__all__ = ["Interaction"]
