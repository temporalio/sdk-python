from __future__ import annotations

from datetime import timedelta
from typing import (
    TYPE_CHECKING,
    Any,
    Mapping,
    Optional,
    Sequence,
    Union,
)

import temporalio.common
from temporalio.nexus.handler._operation_context import TemporalNexusOperationContext
from temporalio.types import (
    MethodAsyncSingleParam,
    ParamType,
    ReturnType,
    SelfType,
)

if TYPE_CHECKING:
    from temporalio.client import Client, WorkflowHandle


# Overload for single-param workflow
async def start_workflow(
    client: Client,
    workflow: MethodAsyncSingleParam[SelfType, ParamType, ReturnType],
    arg: ParamType,
    *,
    id: str,
    task_queue: str,
    execution_timeout: Optional[timedelta] = None,
    run_timeout: Optional[timedelta] = None,
    task_timeout: Optional[timedelta] = None,
    id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    id_conflict_policy: temporalio.common.WorkflowIDConflictPolicy = temporalio.common.WorkflowIDConflictPolicy.UNSPECIFIED,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cron_schedule: str = "",
    memo: Optional[Mapping[str, Any]] = None,
    search_attributes: Optional[
        Union[
            temporalio.common.TypedSearchAttributes,
            temporalio.common.SearchAttributes,
        ]
    ] = None,
    static_summary: Optional[str] = None,
    static_details: Optional[str] = None,
    start_delay: Optional[timedelta] = None,
    start_signal: Optional[str] = None,
    start_signal_args: Sequence[Any] = [],
    rpc_metadata: Mapping[str, str] = {},
    rpc_timeout: Optional[timedelta] = None,
    request_eager_start: bool = False,
    priority: temporalio.common.Priority = temporalio.common.Priority.default,
    versioning_override: Optional[temporalio.common.VersioningOverride] = None,
) -> WorkflowHandle[SelfType, ReturnType]:
    if nexus_ctx := TemporalNexusOperationContext.try_current():
        if nexus_start_ctx := nexus_ctx.temporal_nexus_start_operation_context:
            nexus_completion_callbacks = nexus_start_ctx.get_completion_callbacks()
            workflow_event_links = nexus_start_ctx.get_workflow_event_links()
        else:
            raise RuntimeError(
                "temporalio.nexus.handler.start_workflow() must be called from within a Nexus start operation context"
            )
    else:
        raise RuntimeError(
            "temporalio.nexus.handler.start_workflow() must be called from within a Nexus operation context"
        )

    # We must pass nexus_completion_callbacks and workflow_event_links, but these are
    # deliberately not exposed in overloads, hence the type check violation.
    wf_handle = await client.start_workflow(  # type: ignore
        workflow=workflow,
        arg=arg,
        id=id,
        task_queue=task_queue,
        execution_timeout=execution_timeout,
        run_timeout=run_timeout,
        task_timeout=task_timeout,
        id_reuse_policy=id_reuse_policy,
        id_conflict_policy=id_conflict_policy,
        retry_policy=retry_policy,
        cron_schedule=cron_schedule,
        memo=memo,
        search_attributes=search_attributes,
        static_summary=static_summary,
        static_details=static_details,
        start_delay=start_delay,
        start_signal=start_signal,
        start_signal_args=start_signal_args,
        rpc_metadata=rpc_metadata,
        rpc_timeout=rpc_timeout,
        request_eager_start=request_eager_start,
        priority=priority,
        versioning_override=versioning_override,
        nexus_completion_callbacks=nexus_completion_callbacks,
        workflow_event_links=workflow_event_links,
    )

    nexus_start_ctx.add_outbound_links(wf_handle)

    return wf_handle
