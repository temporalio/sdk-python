import dataclasses
import logging
from collections.abc import Mapping
from typing import Any, Optional

from .handler import _current_context as _current_context
from .handler import workflow_run_operation as workflow_run_operation
from .token import WorkflowOperationToken as WorkflowOperationToken


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
"""Logger that emits additional data describing the current Nexus operation."""
