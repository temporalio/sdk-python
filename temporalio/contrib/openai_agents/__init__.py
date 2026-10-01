"""Compatibility imports for the standalone OpenAI Agents integration.

Install ``temporalio-openai-agents`` and import ``temporalio.openai_agents``
directly in new code.

Legacy ``testing`` and ``workflow`` modules forward the standalone public API.
"""

from temporalio.openai_agents import *  # noqa: F403  # pyright: ignore[reportMissingImports, reportWildcardImportFromLibrary]
from temporalio.openai_agents import (  # pyright: ignore[reportMissingImports]
    __all__ as __all__,
)
