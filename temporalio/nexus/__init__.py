import dataclasses
import logging
from collections.abc import Mapping
from typing import Any, Optional

from .handler import _current_context as _current_context


class LoggerAdapter(logging.LoggerAdapter):
    def __init__(self, logger: logging.Logger, extra: Optional[Mapping[str, Any]]):
        super().__init__(logger, extra or {})

    def process(self, msg: Any, kwargs: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        extra = dict(self.extra or {})
        if context := _current_context.get(None):
            extra.update(
                {f.name: getattr(context, f.name) for f in dataclasses.fields(context)}
            )
        kwargs["extra"] = extra | kwargs.get("extra", {})
        return msg, kwargs


logger = LoggerAdapter(logging.getLogger(__name__), None)
"""Logger that has additional details regarding the current Nexus operation."""
# TODO(dan): add contextual details to logger
