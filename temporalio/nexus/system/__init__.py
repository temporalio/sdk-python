"""Generated system Nexus service models."""

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any, cast

import temporalio.api.common.v1
import temporalio.api.sdk.v1
import temporalio.api.workflowservice.v1
import temporalio.common
import temporalio.converter

from ... import _workflow_requests
from ...bridge._visitor_functions import VisitorFunctions
from ...converter import BinaryProtoPayloadConverter, CompositePayloadConverter
from . import _workflow_service_generated as generated
from ._payload_visitor import PayloadVisitor


class SystemNexusPayloadConverter(CompositePayloadConverter):
    """Payload converter for system Nexus outer envelopes."""

    def __init__(self) -> None:
        """Create a payload converter for system Nexus outer envelopes."""
        super().__init__(BinaryProtoPayloadConverter())


def _set_payload_map(
    target: Any,
    values: Mapping[str, Any],
    payload_converter: temporalio.converter.PayloadConverter,
) -> None:
    for key, value in values.items():
        target[key].CopyFrom(payload_converter.to_payload(value))


def _build_user_metadata(
    payload_converter: temporalio.converter.PayloadConverter,
    static_summary: str | None,
    static_details: str | None,
) -> temporalio.api.sdk.v1.UserMetadata | None:
    if static_summary is None and static_details is None:
        return None
    metadata = temporalio.api.sdk.v1.UserMetadata()
    if static_summary is not None:
        metadata.summary.CopyFrom(payload_converter.to_payload(static_summary))
    if static_details is not None:
        metadata.details.CopyFrom(payload_converter.to_payload(static_details))
    return metadata


def build_signal_with_start_workflow_execution_input(
    *,
    namespace: str,
    workflow_id: str,
    workflow: str,
    workflow_args: Sequence[Any],
    signal: str,
    signal_args: Sequence[Any],
    task_queue: str,
    request_id: str | None,
    payload_converter: temporalio.converter.PayloadConverter,
    execution_timeout: timedelta | None = None,
    run_timeout: timedelta | None = None,
    task_timeout: timedelta | None = None,
    id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    id_conflict_policy: temporalio.common.WorkflowIDConflictPolicy = temporalio.common.WorkflowIDConflictPolicy.UNSPECIFIED,
    retry_policy: temporalio.common.RetryPolicy | None = None,
    cron_schedule: str = "",
    memo: Mapping[str, Any] | None = None,
    search_attributes: temporalio.common.TypedSearchAttributes | None = None,
    static_summary: str | None = None,
    static_details: str | None = None,
    start_delay: timedelta | None = None,
    priority: temporalio.common.Priority = temporalio.common.Priority.default,
    versioning_override: temporalio.common.VersioningOverride | None = None,
) -> temporalio.api.workflowservice.v1.SignalWithStartWorkflowExecutionRequest:
    """Build the system Nexus signal-with-start request."""
    request_memo = None
    if memo is not None:
        request_memo = temporalio.api.common.v1.Memo()
        _set_payload_map(request_memo.fields, memo, payload_converter)
    request_search_attributes = None
    if search_attributes is not None:
        request_search_attributes = temporalio.api.common.v1.SearchAttributes()
        temporalio.converter.encode_search_attributes(
            search_attributes, request_search_attributes
        )
    return _workflow_requests.build_signal_with_start_workflow_execution_request(
        namespace=namespace,
        workflow_id=workflow_id,
        workflow=workflow,
        task_queue=task_queue,
        signal_name=signal,
        workflow_input_payloads=payload_converter.to_payloads(workflow_args)
        if workflow_args
        else (),
        signal_input_payloads=payload_converter.to_payloads(signal_args)
        if signal_args
        else (),
        execution_timeout=execution_timeout,
        run_timeout=run_timeout,
        task_timeout=task_timeout,
        request_id=request_id,
        id_reuse_policy=id_reuse_policy,
        id_conflict_policy=id_conflict_policy,
        retry_policy=retry_policy,
        cron_schedule=cron_schedule,
        memo=request_memo,
        search_attributes=request_search_attributes,
        user_metadata=_build_user_metadata(
            payload_converter, static_summary, static_details
        ),
        start_delay=start_delay,
        priority=priority,
        versioning_override=versioning_override,
    )


async def visit_payload(
    service: str,
    operation: str,
    payload: temporalio.api.common.v1.Payload,
    visitor_functions: VisitorFunctions,
    skip_search_attributes: bool,
) -> temporalio.api.common.v1.Payload | None:
    """Visit nested payloads inside a recognized system Nexus envelope."""
    operation_def = generated.__nexus_operation_registry__.get((service, operation))
    if operation_def is None:
        return None
    input_type = operation_def.input_type
    if not isinstance(input_type, type):
        return None
    payload_converter = get_payload_converter()
    value = payload_converter.from_payload(payload, input_type)
    await PayloadVisitor(skip_search_attributes=skip_search_attributes).visit(
        cast(Any, visitor_functions), value
    )
    return payload_converter.to_payload(value)


def is_system_operation(service: str, operation: str) -> bool:
    """Return whether a Nexus operation uses the generated system envelope."""
    return (service, operation) in generated.__nexus_operation_registry__


def get_payload_converter() -> temporalio.converter.PayloadConverter:
    """Return the fixed payload converter for system Nexus outer envelopes."""
    return SystemNexusPayloadConverter()


__all__ = (
    "build_signal_with_start_workflow_execution_input",
    "generated",
    "get_payload_converter",
    "is_system_operation",
    "SystemNexusPayloadConverter",
    "visit_payload",
)
