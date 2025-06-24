from __future__ import annotations

from datetime import timedelta
from typing import (
    Any,
    Mapping,
    Optional,
    Sequence,
    Union,
)

import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.common
from temporalio.nexus.handler._operation_context import temporal_operation_context
from temporalio.nexus.handler._token import WorkflowOperationToken
from temporalio.types import (
    MethodAsyncSingleParam,
    ParamType,
    ReturnType,
    SelfType,
)


# Overload for single-param workflow
# TODO(nexus-prerelease): bring over other overloads
async def start_workflow(
    workflow: MethodAsyncSingleParam[SelfType, ParamType, ReturnType],
    arg: ParamType,
    *,
    id: str,
    task_queue: Optional[str] = None,
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
) -> WorkflowOperationToken[ReturnType]:
    """Start a workflow that will deliver the result of the Nexus operation.

    The workflow will be started in the same namespace as the Nexus worker, using
    the same client as the worker. If task queue is not specified, the worker's task
    queue will be used.

    See :py:meth:`temporalio.client.Client.start_workflow` for all arguments.

    The return value is :py:class:`temporalio.nexus.handler.WorkflowOperationToken`.
    Use :py:meth:`temporalio.nexus.handler.WorkflowOperationToken.to_workflow_handle`
    to get a :py:class:`temporalio.client.WorkflowHandle` for interacting with the
    workflow.

    The workflow will be started as usual, with the following modifications:

    - On workflow completion, Temporal server will deliver the workflow result to
        the Nexus operation caller, using the callback from the Nexus operation start
        request.

    - The request ID from the Nexus operation start request will be used as the
        request ID for the start workflow request.

    - Inbound links to the caller that were submitted in the Nexus start operation
        request will be attached to the started workflow and, outbound links to the
        started workflow will be added to the Nexus start operation response. If the
        Nexus caller is itself a workflow, this means that the workflow in the caller
        namespace web UI will contain links to the started workflow, and vice versa.
    """
    ctx = temporal_operation_context.get()
    start_operation_context = ctx._temporal_start_operation_context
    if not start_operation_context:
        raise RuntimeError(
            "temporalio.nexus.handler.start_workflow() must be called from "
            "within a Nexus start operation context"
        )

    # TODO(nexus-preview): When sdk-python supports on_conflict_options, Typescript does this:
    # if (workflowOptions.workflowIdConflictPolicy === 'USE_EXISTING') {
    #     internalOptions.onConflictOptions = {
    #     attachLinks: true,
    #     attachCompletionCallbacks: true,
    #     attachRequestId: true,
    #     };
    # }

    # We must pass nexus_completion_callbacks, workflow_event_links, and request_id,
    # but these are deliberately not exposed in overloads, hence the type-check
    # violation.
    wf_handle = await ctx.client.start_workflow(  # type: ignore
        workflow=workflow,
        arg=arg,
        id=id,
        task_queue=task_queue or ctx.task_queue,
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
        nexus_completion_callbacks=start_operation_context.get_completion_callbacks(),
        workflow_event_links=start_operation_context.get_workflow_event_links(),
        request_id=start_operation_context.nexus_operation_context.request_id,
    )

    start_operation_context.add_outbound_links(wf_handle)

    return WorkflowOperationToken[ReturnType]._unsafe_from_workflow_handle(wf_handle)
