from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from tests.worker.workflow_sandbox.testmodules import stateful_module


@workflow.defn
class FromImportPassthroughWorkflow:
    @workflow.run
    async def run(self) -> int:
        return id(stateful_module)
