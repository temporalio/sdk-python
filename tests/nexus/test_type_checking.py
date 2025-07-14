import nexusrpc

import temporalio.nexus
from temporalio import workflow


def _():
    @nexusrpc.handler.service_handler
    class MyService:
        @nexusrpc.handler.sync_operation
        async def my_sync_operation(
            self, ctx: nexusrpc.handler.StartOperationContext, input: int
        ) -> str:
            raise NotImplementedError

        @temporalio.nexus.workflow_run_operation
        async def my_workflow_run_operation(
            self, ctx: temporalio.nexus.WorkflowRunOperationContext, input: int
        ) -> temporalio.nexus.WorkflowHandle[str]:
            raise NotImplementedError

    @workflow.defn(sandboxed=False)
    class MyWorkflow:
        @workflow.run
        async def invoke_nexus_op_and_assert_error(self) -> None:
            nexus_client = workflow.create_nexus_client(
                service=MyService,
                endpoint="fake-endpoint",
            )
            await nexus_client.execute_operation(MyService.my_sync_operation, 1)
            await nexus_client.execute_operation(MyService.my_workflow_run_operation, 1)
