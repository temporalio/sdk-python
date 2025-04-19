import logging
from collections.abc import Mapping
from typing import Any, Optional


class LoggerAdapter(logging.LoggerAdapter):
    def __init__(self, logger: logging.Logger, extra: Optional[Mapping[str, Any]]):
        super().__init__(logger, extra or {})


logger = LoggerAdapter(logging.getLogger(__name__), None)
"""Logger that has additional details regarding the current Nexus operation."""
