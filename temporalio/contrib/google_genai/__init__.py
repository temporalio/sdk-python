"""Compatibility imports for the standalone Google GenAI integration.

Install ``temporalio-google-genai`` and import ``temporalio.google_genai``
directly in new code.

Legacy ``testing`` and ``workflow`` modules forward the standalone public API.
"""

from temporalio.google_genai import *  # noqa: F403  # pyright: ignore[reportMissingImports, reportWildcardImportFromLibrary]
from temporalio.google_genai import (  # pyright: ignore[reportMissingImports]
    __all__ as __all__,
)
