import logging
from typing import (
    Any,
    Mapping,
    MutableMapping,
    Optional,
)

from ._decorators import workflow_run_operation as workflow_run_operation
from ._operation_context import (
    _TemporalNexusOperationContext as _TemporalNexusOperationContext,
)
from ._operation_context import temporal_operation_context as temporal_operation_context
from ._operation_handlers import cancel_operation as cancel_operation
from ._token import WorkflowHandle as WorkflowHandle
from ._workflow import start_workflow as start_workflow


class LoggerAdapter(logging.LoggerAdapter):
    def __init__(self, logger: logging.Logger, extra: Optional[Mapping[str, Any]]):
        super().__init__(logger, extra or {})

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        extra = dict(self.extra or {})
        if tctx := temporal_operation_context.get(None):
            extra["service"] = tctx.nexus_operation_context.service
            extra["operation"] = tctx.nexus_operation_context.operation
            extra["task_queue"] = tctx.task_queue
        kwargs["extra"] = extra | kwargs.get("extra", {})
        return msg, kwargs


logger = LoggerAdapter(logging.getLogger(__name__), None)
"""Logger that emits additional data describing the current Nexus operation."""

# TODO(nexus-prerelease) WARN temporal_sdk_core::worker::nexus: Failed to parse nexus timeout header value '9.155416ms'
# 2025-06-25T12:58:05.749589Z  WARN temporal_sdk_core::worker::nexus: Failed to parse nexus timeout header value '9.155416ms'
# 2025-06-25T12:58:05.763052Z  WARN temporal_sdk_core::worker::nexus: Nexus task not found on completion. This may happen if the operation has already been cancelled but completed anyway. details=Status { code: NotFound, message: "Nexus task not found or already expired", details: b"\x08\x05\x12'Nexus task not found or already expired\x1aB\n@type.googleapis.com/temporal.api.errordetails.v1.NotFoundFailure", metadata: MetadataMap { headers: {"content-type": "application/grpc"} }, source: None }
