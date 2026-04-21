"""Generated system Nexus service models.

This package contains code generated from Temporal's system Nexus schemas.
Higher-level ergonomic APIs may wrap these generated types.
"""

import dataclasses
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import timedelta
from enum import Enum
from typing import Any, cast

import google.protobuf.message
from google.protobuf.json_format import MessageToDict, Parse, ParseDict

import temporalio.api.common.v1
import temporalio.common
import temporalio.converter

from ...converter import CompositePayloadConverter, JSONProtoPayloadConverter
from ...converter._payload_converter import value_to_type
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

_WORKFLOW_ID_REUSE_POLICY_TO_GENERATED = {
    temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE: generated.WorkflowIDReusePolicy.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE,
    temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY: generated.WorkflowIDReusePolicy.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE_FAILED_ONLY,
    temporalio.common.WorkflowIDReusePolicy.REJECT_DUPLICATE: generated.WorkflowIDReusePolicy.WORKFLOW_ID_REUSE_POLICY_REJECT_DUPLICATE,
    temporalio.common.WorkflowIDReusePolicy.TERMINATE_IF_RUNNING: generated.WorkflowIDReusePolicy.WORKFLOW_ID_REUSE_POLICY_TERMINATE_IF_RUNNING,
}

_WORKFLOW_ID_CONFLICT_POLICY_TO_GENERATED = {
    temporalio.common.WorkflowIDConflictPolicy.UNSPECIFIED: generated.WorkflowIDConflictPolicy.WORKFLOW_ID_CONFLICT_POLICY_UNSPECIFIED,
    temporalio.common.WorkflowIDConflictPolicy.FAIL: generated.WorkflowIDConflictPolicy.WORKFLOW_ID_CONFLICT_POLICY_FAIL,
    temporalio.common.WorkflowIDConflictPolicy.USE_EXISTING: generated.WorkflowIDConflictPolicy.WORKFLOW_ID_CONFLICT_POLICY_USE_EXISTING,
    temporalio.common.WorkflowIDConflictPolicy.TERMINATE_EXISTING: generated.WorkflowIDConflictPolicy.WORKFLOW_ID_CONFLICT_POLICY_TERMINATE_EXISTING,
}


class _SystemNexusJSONProtoPayloadConverter(JSONProtoPayloadConverter):
    def to_payload(self, value: Any) -> temporalio.api.common.v1.Payload | None:
        proto_type = _get_generated_proto_type(value)
        if proto_type is not None:
            return super().to_payload(
                ParseDict(
                    dataclasses.asdict(value),
                    proto_type(),
                    ignore_unknown_fields=True,
                )
            )
        return super().to_payload(value)

    def from_payload(
        self,
        payload: temporalio.api.common.v1.Payload,
        type_hint: type | None = None,
    ) -> Any:
        proto_type = _get_generated_proto_type(type_hint)
        if proto_type is not None and type_hint is not None:
            proto_value = Parse(
                payload.data,
                proto_type(),
                ignore_unknown_fields=True,
            )
            return value_to_type(
                type_hint,
                MessageToDict(proto_value, preserving_proto_field_name=True),
                [_SystemNexusStrEnumConverter()],
            )
        return super().from_payload(payload, type_hint)


class SystemNexusPayloadConverter(CompositePayloadConverter):
    """Payload converter for system Nexus outer envelopes."""

    def __init__(self) -> None:
        """Create a payload converter for system Nexus outer envelopes."""
        super().__init__(_SystemNexusJSONProtoPayloadConverter())


def _get_generated_proto_type(
    value_or_type: Any,
) -> type[google.protobuf.message.Message] | None:
    candidate = (
        value_or_type if isinstance(value_or_type, type) else type(value_or_type)
    )
    proto_type = getattr(candidate, "__temporal_nexus_proto_type__", None)
    if isinstance(proto_type, type) and issubclass(
        proto_type, google.protobuf.message.Message
    ):
        return proto_type
    return None


class _SystemNexusStrEnumConverter(temporalio.converter.JSONTypeConverter):
    # Generated enums subclass str and Enum, not StrEnum, so the default
    # value_to_type enum handling does not reconstruct them.
    def to_typed_value(self, hint: type, value: Any) -> Any:
        if isinstance(hint, type) and issubclass(hint, Enum) and issubclass(hint, str):
            if not isinstance(value, str):
                raise TypeError(f"Expected value to be str, was {type(value)}")
            return hint(value)
        return temporalio.converter.JSONTypeConverter.Unhandled


def _payload_to_json_value(
    converter: temporalio.converter.PayloadConverter, value: Any
) -> generated.Payload:
    return _proto_payload_to_generated(converter.to_payload(value))


def _proto_payload_to_generated(
    payload: temporalio.api.common.v1.Payload,
) -> generated.Payload:
    value = MessageToDict(payload)
    return generated.Payload(
        data=cast("str | None", value.get("data")),
        external_payloads=[
            generated.PayloadExternalPayloadDetails(**details)
            for details in cast(
                "list[dict[str, str]]", value.get("externalPayloads", [])
            )
        ]
        or None,
        metadata=cast("dict[str, str] | None", value.get("metadata")),
    )


def _payloads_to_input(
    converter: temporalio.converter.PayloadConverter, values: Sequence[Any]
) -> generated.Payloads | None:
    payloads = converter.to_payloads(values) if values else []
    if not payloads:
        return None
    return generated.Payloads(
        payloads=[_proto_payload_to_generated(payload) for payload in payloads]
    )


def _search_attributes_to_json_map(
    attributes: temporalio.common.TypedSearchAttributes,
) -> dict[str, generated.Payload]:
    return {
        pair.key.name: _proto_payload_to_generated(
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
        initial_interval=f"{retry_policy.initial_interval.total_seconds()}s",
        backoff_coefficient=retry_policy.backoff_coefficient,
        maximum_interval=f"{(retry_policy.maximum_interval or retry_policy.initial_interval * 100).total_seconds()}s",
        maximum_attempts=retry_policy.maximum_attempts,
        non_retryable_error_types=(
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
        priority_key=priority.priority_key,
        fairness_key=priority.fairness_key,
        fairness_weight=priority.fairness_weight,
    )


def _workflow_id_reuse_policy_to_generated(
    policy: temporalio.common.WorkflowIDReusePolicy,
) -> generated.WorkflowIDReusePolicy:
    return _WORKFLOW_ID_REUSE_POLICY_TO_GENERATED[policy]


def _workflow_id_conflict_policy_to_generated(
    policy: temporalio.common.WorkflowIDConflictPolicy,
) -> generated.WorkflowIDConflictPolicy:
    return _WORKFLOW_ID_CONFLICT_POLICY_TO_GENERATED[policy]


def _versioning_override_to_generated(
    versioning_override: temporalio.common.VersioningOverride,
) -> generated.VersioningOverride:
    if isinstance(versioning_override, temporalio.common.AutoUpgradeVersioningOverride):
        return generated.VersioningOverride(
            auto_upgrade=True,
            behavior=generated.VersioningOverrideBehavior.VERSIONING_BEHAVIOR_AUTO_UPGRADE,
        )
    if isinstance(versioning_override, temporalio.common.PinnedVersioningOverride):
        return generated.VersioningOverride(
            behavior=generated.VersioningOverrideBehavior.VERSIONING_BEHAVIOR_PINNED,
            pinned_version=versioning_override.version.to_canonical_string(),
            pinned=generated.VersioningOverridePinnedOverride(
                behavior=generated.VersioningOverridePinnedOverrideBehavior.PINNED_OVERRIDE_BEHAVIOR_PINNED,
                version=generated.WorkerDeploymentVersion(
                    deployment_name=versioning_override.version.deployment_name,
                    build_id=versioning_override.version.build_id,
                ),
            ),
            deployment=generated.Deployment(
                series_name=versioning_override.version.deployment_name,
                build_id=versioning_override.version.build_id,
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
) -> generated.SignalWithStartWorkflowExecutionRequest:
    """Build the generated system Nexus input for signal-with-start."""
    return generated.SignalWithStartWorkflowExecutionRequest(
        namespace=namespace,
        workflow_id=workflow_id,
        workflow_type=generated.WorkflowType(name=workflow),
        task_queue=generated.TaskQueue(name=task_queue),
        input=_payloads_to_input(payload_converter, workflow_args),
        workflow_execution_timeout=(
            f"{execution_timeout.total_seconds()}s" if execution_timeout else None
        ),
        workflow_run_timeout=f"{run_timeout.total_seconds()}s" if run_timeout else None,
        workflow_task_timeout=(
            f"{task_timeout.total_seconds()}s" if task_timeout else None
        ),
        request_id=request_id,
        workflow_id_reuse_policy=_workflow_id_reuse_policy_to_generated(
            id_reuse_policy
        ),
        workflow_id_conflict_policy=(
            _workflow_id_conflict_policy_to_generated(id_conflict_policy)
            if id_conflict_policy
            != temporalio.common.WorkflowIDConflictPolicy.UNSPECIFIED
            else None
        ),
        retry_policy=(
            _retry_policy_to_generated(retry_policy) if retry_policy else None
        ),
        cron_schedule=cron_schedule or None,
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
        search_attributes=(
            generated.SearchAttributes(
                indexed_fields=_search_attributes_to_json_map(search_attributes)
            )
            if search_attributes
            else None
        ),
        signal_name=signal,
        signal_input=_payloads_to_input(payload_converter, signal_args),
        user_metadata=(
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
        workflow_start_delay=(
            f"{start_delay.total_seconds()}s" if start_delay else None
        ),
        priority=_priority_to_generated(priority),
        versioning_override=(
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
    return SystemNexusPayloadConverter()


__all__ = (
    "build_signal_with_start_workflow_execution_input",
    "generated",
    "get_payload_converter",
    "get_payload_visitor",
    "is_system_operation",
    "SystemNexusPayloadConverter",
)
