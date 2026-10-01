"""Compatibility imports for the standalone OpenAI Agents integration.

Install ``temporalio-openai-agents`` and import ``temporalio.openai_agents``
directly in new code.

Legacy submodule imports resolve to the standalone modules.
"""

import sys as _sys
from types import ModuleType as _ModuleType

import temporalio.openai_agents as _standalone  # pyright: ignore[reportMissingImports]
from temporalio.openai_agents import *  # noqa: F403  # pyright: ignore[reportMissingImports, reportWildcardImportFromLibrary]
from temporalio.openai_agents import (  # pyright: ignore[reportMissingImports]
    __all__ as __all__,
)

# Direct imports of the old submodule paths must work without separate shim files.
for _name in __all__:
    _module = getattr(_standalone, _name)
    if isinstance(_module, _ModuleType):
        _sys.modules[f"{__name__}.{_name}"] = _module
