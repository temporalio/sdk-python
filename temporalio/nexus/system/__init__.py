"""Generated system Nexus service models.

This package contains code generated from Temporal's system Nexus schemas.
Higher-level ergonomic APIs may wrap these generated types.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import timedelta
from typing import Any, cast

from google.protobuf.json_format import MessageToDict

import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.common
import temporalio.converter

from . import _workflow_service_generated as generated
from ._workflow_service_generated import __temporal_nexus_payload_visitors__

TemporalNexusPayloadVisitor = Callable[
    [
        temporalio.api.common.v1.Payload,
        Callable[
            [Sequence[temporalio.api.common.v1.Payload]],
            Awaitable[list[temporalio.api.common.v1.Payload]],
        ],
        bool,
    ],
    Awaitable[temporalio.api.common.v1.Payload],
]

_SYSTEM_NEXUS_PAYLOAD_CONVERTER = temporalio.converter.default().payload_converter


def _payload_to_json_value(
    converter: temporalio.converter.PayloadConverter, value: Any
) -> dict[str, Any]:
    return MessageToDict(converter.to_payload(value))


def _payloads_to_input(
    converter: temporalio.converter.PayloadConverter, values: Sequence[Any]
) -> generated.Input | None:
    payloads = converter.to_payloads(values) if values else []
    if not payloads:
        return None
    return generated.Input(payloads=[MessageToDict(payload) for payload in payloads])


def _search_attributes_to_json_map(
    attributes: temporalio.common.TypedSearchAttributes,
) -> dict[str, Any]:
    return {
        pair.key.name: MessageToDict(
            temporalio.converter.encode_typed_search_attribute_value(
                pair.key, pair.value
            )
        )
        for pair in attributes
    }


def _retry_policy_to_generated(
    retry_policy: temporalio.common.RetryPolicy,
) -> generated.RetryPolicy:
    retry_policy._validate()
    return generated.RetryPolicy(
        initialInterval=f"{retry_policy.initial_interval.total_seconds()}s",
        backoffCoefficient=retry_policy.backoff_coefficient,
        maximumInterval=f"{(retry_policy.maximum_interval or retry_policy.initial_interval * 100).total_seconds()}s",
        maximumAttempts=retry_policy.maximum_attempts,
        nonRetryableErrorTypes=(
            list(retry_policy.non_retryable_error_types)
            if retry_policy.non_retryable_error_types
            else None
        ),
    )


def _priority_to_generated(
    priority: temporalio.common.Priority,
) -> generated.Priority | None:
    if (
        priority.priority_key is None
        and priority.fairness_key is None
        and priority.fairness_weight is None
    ):
        return None
    return generated.Priority(
        priorityKey=priority.priority_key,
        fairnessKey=priority.fairness_key,
        fairnessWeight=priority.fairness_weight,
    )


def _workflow_id_reuse_policy_to_generated(
    policy: temporalio.common.WorkflowIDReusePolicy,
) -> generated.WorkflowIDReusePolicy:
    return generated.WorkflowIDReusePolicy(
        temporalio.api.enums.v1.WorkflowIdReusePolicy.Name(
            cast("temporalio.api.enums.v1.WorkflowIdReusePolicy.ValueType", int(policy))
        )
    )


def _workflow_id_conflict_policy_to_generated(
    policy: temporalio.common.WorkflowIDConflictPolicy,
) -> generated.WorkflowIDConflictPolicy:
    return generated.WorkflowIDConflictPolicy(
        temporalio.api.enums.v1.WorkflowIdConflictPolicy.Name(
            cast(
                "temporalio.api.enums.v1.WorkflowIdConflictPolicy.ValueType",
                int(policy),
            )
        )
    )


def _versioning_override_to_generated(
    versioning_override: temporalio.common.VersioningOverride,
) -> generated.VersioningOverride:
    if isinstance(versioning_override, temporalio.common.AutoUpgradeVersioningOverride):
        return generated.VersioningOverride(
            autoUpgrade=True,
            behavior=generated.VersioningOverrideBehavior.VERSIONING_BEHAVIOR_AUTO_UPGRADE,
        )
    if isinstance(versioning_override, temporalio.common.PinnedVersioningOverride):
        return generated.VersioningOverride(
            behavior=generated.VersioningOverrideBehavior.VERSIONING_BEHAVIOR_PINNED,
            pinnedVersion=versioning_override.version.to_canonical_string(),
            pinned=generated.Pinned(
                behavior=generated.PinnedBehavior.PINNED_OVERRIDE_BEHAVIOR_PINNED,
                version=generated.Version(
                    deploymentName=versioning_override.version.deployment_name,
                    buildId=versioning_override.version.build_id,
                ),
            ),
            deployment=generated.Deployment(
                seriesName=versioning_override.version.deployment_name,
                buildId=versioning_override.version.build_id,
            ),
        )
    raise TypeError(
        f"Unsupported versioning override type: {type(versioning_override)!r}"
    )


def build_signal_with_start_workflow_execution_input(
    *,
    namespace: str,
    workflow_id: str,
    workflow: str,
    workflow_args: Sequence[Any],
    signal: str,
    signal_args: Sequence[Any],
    task_queue: str,
    request_id: str,
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
) -> generated.WorkflowServiceSignalWithStartWorkflowExecutionInput:
    """Build the generated system Nexus input for signal-with-start."""
    return generated.WorkflowServiceSignalWithStartWorkflowExecutionInput(
        namespace=namespace,
        workflowId=workflow_id,
        workflowType=generated.WorkflowType(name=workflow),
        taskQueue=generated.TaskQueue(name=task_queue),
        input=_payloads_to_input(payload_converter, workflow_args),
        workflowExecutionTimeout=(
            f"{execution_timeout.total_seconds()}s" if execution_timeout else None
        ),
        workflowRunTimeout=f"{run_timeout.total_seconds()}s" if run_timeout else None,
        workflowTaskTimeout=(
            f"{task_timeout.total_seconds()}s" if task_timeout else None
        ),
        requestId=request_id,
        workflowIdReusePolicy=_workflow_id_reuse_policy_to_generated(id_reuse_policy),
        workflowIdConflictPolicy=_workflow_id_conflict_policy_to_generated(
            id_conflict_policy
        ),
        retryPolicy=(
            _retry_policy_to_generated(retry_policy) if retry_policy else None
        ),
        cronSchedule=cron_schedule,
        memo=(
            generated.Memo(
                fields={
                    key: _payload_to_json_value(payload_converter, value)
                    for key, value in memo.items()
                }
            )
            if memo
            else None
        ),
        searchAttributes=(
            generated.SearchAttributes(
                indexedFields=_search_attributes_to_json_map(search_attributes)
            )
            if search_attributes
            else None
        ),
        signalName=signal,
        signalInput=_payloads_to_input(payload_converter, signal_args),
        userMetadata=(
            generated.UserMetadata(
                summary=_payload_to_json_value(payload_converter, static_summary)
                if static_summary is not None
                else None,
                details=_payload_to_json_value(payload_converter, static_details)
                if static_details is not None
                else None,
            )
            if static_summary is not None or static_details is not None
            else None
        ),
        workflowStartDelay=(f"{start_delay.total_seconds()}s" if start_delay else None),
        priority=_priority_to_generated(priority),
        versioningOverride=(
            _versioning_override_to_generated(versioning_override)
            if versioning_override
            else None
        ),
    )


def get_payload_visitor(
    service: str,
    operation: str,
) -> TemporalNexusPayloadVisitor | None:
    """Return the generated nested-payload visitor for a system Nexus operation."""
    return __temporal_nexus_payload_visitors__.get((service, operation))


def is_system_operation(service: str, operation: str) -> bool:
    """Return whether a Nexus operation uses the generated system envelope."""
    return get_payload_visitor(service, operation) is not None


def get_payload_converter() -> temporalio.converter.PayloadConverter:
    """Return the fixed payload converter for system Nexus outer envelopes."""
    return _SYSTEM_NEXUS_PAYLOAD_CONVERTER


__all__ = (
    "build_signal_with_start_workflow_execution_input",
    "generated",
    "get_payload_converter",
    "get_payload_visitor",
    "is_system_operation",
)
