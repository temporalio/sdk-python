"""Compatibility imports for the standalone Google ADK integration.

Install ``temporalio-google-adk`` and import ``temporalio.google_adk``
directly in new code.

Legacy ``workflow`` module forwards the standalone public API.
"""

from temporalio.google_adk import *  # noqa: F403  # pyright: ignore[reportMissingImports, reportWildcardImportFromLibrary]
from temporalio.google_adk import (  # pyright: ignore[reportMissingImports]
    __all__ as __all__,
)
