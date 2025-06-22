from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import (
    TYPE_CHECKING,
    Any,
    MutableMapping,
    Optional,
)

from nexusrpc.handler import (
    CancelOperationContext as CancelOperationContext,
)
from nexusrpc.handler import (
    HandlerError as HandlerError,
)
from nexusrpc.handler import (
    HandlerErrorType as HandlerErrorType,
)

from ._operation_context import TemporalOperationContext as TemporalOperationContext
from ._operation_handlers import (
    WorkflowRunOperationHandler as WorkflowRunOperationHandler,
)
from ._operation_handlers import cancel_operation as cancel_operation
from ._operation_handlers import (
    workflow_run_operation_handler as workflow_run_operation_handler,
)
from ._token import (
    WorkflowOperationToken as WorkflowOperationToken,
)

if TYPE_CHECKING:
    from temporalio.client import (
        Client as Client,
    )
    from temporalio.client import (
        WorkflowHandle as WorkflowHandle,
    )


class LoggerAdapter(logging.LoggerAdapter):
    def __init__(self, logger: logging.Logger, extra: Optional[Mapping[str, Any]]):
        super().__init__(logger, extra or {})

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        extra = dict(self.extra or {})
        if tctx := TemporalOperationContext.current():
            extra["service"] = tctx.nexus_operation_context.service
            extra["operation"] = tctx.nexus_operation_context.operation
            extra["task_queue"] = tctx.task_queue
        kwargs["extra"] = extra | kwargs.get("extra", {})
        return msg, kwargs


logger = LoggerAdapter(logging.getLogger(__name__), None)
"""Logger that emits additional data describing the current Nexus operation."""
