import logging
from collections.abc import Mapping
from typing import Any, MutableMapping, Optional

from .context import CancelOperationContext as CancelOperationContext
from .context import Context as Context
from .context import StartOperationContext as StartOperationContext
from .context import current_context
from .handler import workflow_run_operation_handler as workflow_run_operation_handler
from .token import WorkflowOperationToken as WorkflowOperationToken


class LoggerAdapter(logging.LoggerAdapter):
    def __init__(self, logger: logging.Logger, extra: Optional[Mapping[str, Any]]):
        super().__init__(logger, extra or {})

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        extra = dict(self.extra or {})
        if ctx := current_context.get(None):
            extra["service"] = ctx.operation_context.service
            extra["operation"] = ctx.operation_context.operation
            extra["task_queue"] = ctx.operation_context.task_queue
        kwargs["extra"] = extra | kwargs.get("extra", {})
        return msg, kwargs


logger = LoggerAdapter(logging.getLogger(__name__), None)
"""Logger that emits additional data describing the current Nexus operation."""
