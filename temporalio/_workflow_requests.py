from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import cast

import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.api.sdk.v1
import temporalio.api.workflowservice.v1
import temporalio.common


def populate_start_workflow_execution_request(
    req: (
        temporalio.api.workflowservice.v1.StartWorkflowExecutionRequest
        | temporalio.api.workflowservice.v1.SignalWithStartWorkflowExecutionRequest
    ),
    *,
    namespace: str,
    workflow_id: str,
    workflow: str,
    task_queue: str,
    input_payloads: Sequence[temporalio.api.common.v1.Payload] = (),
    execution_timeout: timedelta | None = None,
    run_timeout: timedelta | None = None,
    task_timeout: timedelta | None = None,
    identity: str | None = None,
    request_id: str | None = None,
    id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    id_conflict_policy: temporalio.common.WorkflowIDConflictPolicy = temporalio.common.WorkflowIDConflictPolicy.UNSPECIFIED,
    retry_policy: temporalio.common.RetryPolicy | None = None,
    cron_schedule: str = "",
    memo: temporalio.api.common.v1.Memo | None = None,
    search_attributes: temporalio.api.common.v1.SearchAttributes | None = None,
    header: temporalio.api.common.v1.Header | None = None,
    user_metadata: temporalio.api.sdk.v1.UserMetadata | None = None,
    start_delay: timedelta | None = None,
    priority: temporalio.common.Priority | None = None,
    versioning_override: temporalio.common.VersioningOverride | None = None,
) -> None:
    """Populate a workflow-service start-style request from pre-encoded pieces."""
    req.namespace = namespace
    req.workflow_id = workflow_id
    req.workflow_type.name = workflow
    req.task_queue.name = task_queue
    if input_payloads:
        req.input.payloads.extend(input_payloads)
    if execution_timeout is not None:
        req.workflow_execution_timeout.FromTimedelta(execution_timeout)
    if run_timeout is not None:
        req.workflow_run_timeout.FromTimedelta(run_timeout)
    if task_timeout is not None:
        req.workflow_task_timeout.FromTimedelta(task_timeout)
    if identity is not None:
        req.identity = identity
    if request_id is not None:
        req.request_id = request_id
    req.workflow_id_reuse_policy = cast(
        "temporalio.api.enums.v1.WorkflowIdReusePolicy.ValueType",
        int(id_reuse_policy),
    )
    req.workflow_id_conflict_policy = cast(
        "temporalio.api.enums.v1.WorkflowIdConflictPolicy.ValueType",
        int(id_conflict_policy),
    )
    if retry_policy is not None:
        retry_policy.apply_to_proto(req.retry_policy)
    req.cron_schedule = cron_schedule
    if memo is not None:
        req.memo.CopyFrom(memo)
    if search_attributes is not None:
        req.search_attributes.CopyFrom(search_attributes)
    if header is not None:
        req.header.CopyFrom(header)
    if user_metadata is not None:
        req.user_metadata.CopyFrom(user_metadata)
    if start_delay is not None:
        req.workflow_start_delay.FromTimedelta(start_delay)
    if priority is not None:
        req.priority.CopyFrom(priority._to_proto())
    if versioning_override is not None:
        req.versioning_override.CopyFrom(versioning_override._to_proto())


def build_signal_with_start_workflow_execution_request(
    *,
    namespace: str,
    workflow_id: str,
    workflow: str,
    task_queue: str,
    signal_name: str,
    workflow_input_payloads: Sequence[temporalio.api.common.v1.Payload] = (),
    signal_input_payloads: Sequence[temporalio.api.common.v1.Payload] = (),
    execution_timeout: timedelta | None = None,
    run_timeout: timedelta | None = None,
    task_timeout: timedelta | None = None,
    identity: str | None = None,
    request_id: str | None = None,
    id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    id_conflict_policy: temporalio.common.WorkflowIDConflictPolicy = temporalio.common.WorkflowIDConflictPolicy.UNSPECIFIED,
    retry_policy: temporalio.common.RetryPolicy | None = None,
    cron_schedule: str = "",
    memo: temporalio.api.common.v1.Memo | None = None,
    search_attributes: temporalio.api.common.v1.SearchAttributes | None = None,
    header: temporalio.api.common.v1.Header | None = None,
    user_metadata: temporalio.api.sdk.v1.UserMetadata | None = None,
    start_delay: timedelta | None = None,
    priority: temporalio.common.Priority | None = None,
    versioning_override: temporalio.common.VersioningOverride | None = None,
) -> temporalio.api.workflowservice.v1.SignalWithStartWorkflowExecutionRequest:
    """Build a signal-with-start workflow-service request from pre-encoded pieces."""
    req = temporalio.api.workflowservice.v1.SignalWithStartWorkflowExecutionRequest(
        signal_name=signal_name
    )
    if signal_input_payloads:
        req.signal_input.payloads.extend(signal_input_payloads)
    populate_start_workflow_execution_request(
        req,
        namespace=namespace,
        workflow_id=workflow_id,
        workflow=workflow,
        task_queue=task_queue,
        input_payloads=workflow_input_payloads,
        execution_timeout=execution_timeout,
        run_timeout=run_timeout,
        task_timeout=task_timeout,
        identity=identity,
        request_id=request_id,
        id_reuse_policy=id_reuse_policy,
        id_conflict_policy=id_conflict_policy,
        retry_policy=retry_policy,
        cron_schedule=cron_schedule,
        memo=memo,
        search_attributes=search_attributes,
        header=header,
        user_metadata=user_metadata,
        start_delay=start_delay,
        priority=priority,
        versioning_override=versioning_override,
    )
    return req
