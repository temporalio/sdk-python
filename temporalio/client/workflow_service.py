from temporalio.api.workflowservice.v1 import (
    StartWorkflowExecutionRequest,
    StartWorkflowExecutionResponse,
)


class WorkflowService:
    def __init__(self):
        raise NotImplementedError

    async def start_workflow_execution(
        self, request: StartWorkflowExecutionRequest
    ) -> StartWorkflowExecutionResponse:
        raise NotImplementedError
