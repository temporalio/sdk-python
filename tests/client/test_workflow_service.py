import uuid

import temporalio.api.common.v1
import temporalio.api.taskqueue.v1
import temporalio.api.workflowservice.v1
import temporalio.client
import temporalio.converter


async def test_simple():
    service = await temporalio.client.WorkflowService.connect("http://localhost:7233")
    task_queue = f"my-task-queue-{uuid.uuid4()}"
    workflow_id = f"my-workflow-{uuid.uuid4()}"
    resp = await service.start_workflow_execution(
        temporalio.api.workflowservice.v1.StartWorkflowExecutionRequest(
            namespace="default",
            workflow_id=workflow_id,
            workflow_type=temporalio.api.common.v1.WorkflowType(name="my-workflow"),
            task_queue=temporalio.api.taskqueue.v1.TaskQueue(name=task_queue),
            input=temporalio.api.common.v1.Payloads(
                payloads=await temporalio.converter.default().encode(["some string!"])
            ),
            request_id=str(uuid.uuid4()),
        )
    )
    print(f"Started workflow with run ID: {resp.run_id}")
