"""Compatibility imports for the standalone Deep Agents integration.

Install ``temporalio-deepagents`` and import ``temporalio.deepagents``
directly in new code.

Legacy ``testing`` and ``workflow`` modules forward the standalone public API.
"""

from temporalio.deepagents import *  # noqa: F403  # pyright: ignore[reportMissingImports, reportWildcardImportFromLibrary]
from temporalio.deepagents import (  # pyright: ignore[reportMissingImports]
    __all__ as __all__,
)
