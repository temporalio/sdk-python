"""Compatibility imports for the standalone Strands Agents integration.

Install ``temporalio-strands-agents`` and import ``temporalio.strands_agents``
directly in new code.

Legacy ``workflow`` module forwards the standalone public API.
"""

from temporalio.strands_agents import *  # noqa: F403  # pyright: ignore[reportMissingImports, reportWildcardImportFromLibrary]
from temporalio.strands_agents import (  # pyright: ignore[reportMissingImports]
    __all__ as __all__,
)
