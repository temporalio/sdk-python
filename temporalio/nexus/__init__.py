import contextvars
import dataclasses
import logging
from collections.abc import Mapping
from typing import Any, Optional

from .handler import _Context

_current_context: contextvars.ContextVar[_Context] = contextvars.ContextVar("activity")


class LoggerAdapter(logging.LoggerAdapter):
    def __init__(self, logger: logging.Logger, extra: Optional[Mapping[str, Any]]):
        super().__init__(logger, extra or {})

    def process(self, msg: Any, kwargs: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        extra = dict(self.extra or {})
        if context := _current_context.get(None):
            extra.update(dataclasses.asdict(context))
        kwargs["extra"] = extra | kwargs.get("extra", {})
        return msg, kwargs


logger = LoggerAdapter(logging.getLogger(__name__), None)
"""Logger that has additional details regarding the current Nexus operation."""
# TODO(dan): add contextual details to logger
