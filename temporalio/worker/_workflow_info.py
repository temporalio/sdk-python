"""Workflow information derived solely from a Core activation."""

from datetime import timezone

import temporalio.bridge.proto.workflow_activation
import temporalio.common
import temporalio.converter
import temporalio.workflow


def workflow_info(
    namespace: str,
    task_queue: str,
    act: temporalio.bridge.proto.workflow_activation.WorkflowActivation,
    init: temporalio.bridge.proto.workflow_activation.InitializeWorkflow,
) -> temporalio.workflow.Info:
    """Read workflow metadata shared by the native and WASM runners."""
    parent: temporalio.workflow.ParentInfo | None = None
    root: temporalio.workflow.RootInfo | None = None
    if init.HasField("parent_workflow_info"):
        parent = temporalio.workflow.ParentInfo(
            namespace=init.parent_workflow_info.namespace,
            run_id=init.parent_workflow_info.run_id,
            workflow_id=init.parent_workflow_info.workflow_id,
        )
    if init.HasField("root_workflow"):
        root = temporalio.workflow.RootInfo(
            run_id=init.root_workflow.run_id,
            workflow_id=init.root_workflow.workflow_id,
        )
    return temporalio.workflow.Info(
        attempt=init.attempt,
        continued_run_id=init.continued_from_execution_run_id or None,
        cron_schedule=init.cron_schedule or None,
        execution_timeout=init.workflow_execution_timeout.ToTimedelta()
        if init.HasField("workflow_execution_timeout")
        else None,
        first_execution_run_id=init.first_execution_run_id,
        headers=dict(init.headers),
        namespace=namespace,
        original_execution_run_id=init.original_execution_run_id or act.run_id,
        parent=parent,
        root=root,
        raw_memo=dict(init.memo.fields),
        retry_policy=temporalio.common.RetryPolicy.from_proto(init.retry_policy)
        if init.HasField("retry_policy")
        else None,
        run_id=act.run_id,
        run_timeout=init.workflow_run_timeout.ToTimedelta()
        if init.HasField("workflow_run_timeout")
        else None,
        search_attributes=temporalio.converter.decode_search_attributes(
            init.search_attributes
        ),
        start_time=act.timestamp.ToDatetime().replace(tzinfo=timezone.utc),
        workflow_start_time=init.start_time.ToDatetime().replace(tzinfo=timezone.utc),
        task_queue=task_queue,
        task_timeout=init.workflow_task_timeout.ToTimedelta(),
        typed_search_attributes=temporalio.converter.decode_typed_search_attributes(
            init.search_attributes
        ),
        workflow_id=init.workflow_id,
        workflow_type=init.workflow_type,
        priority=temporalio.common.Priority._from_proto(init.priority),
    )
