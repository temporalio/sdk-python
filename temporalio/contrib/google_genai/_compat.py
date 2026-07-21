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
    # variants. At runtime we get the resource class but type checkers
    # resolve to the alias which causes failures (the alias has no ``id``,
    # etc.). Force the type checker to see the class' defining module (in
    # the same location in 2.10 and 2.12).
    from google.genai._gaos.types.interactions.interaction import Interaction
else:
    # At runtime, Interaction resolves properly for 2.10 and 2.12
    from google.genai.interactions import Interaction

__all__ = ["Interaction"]
