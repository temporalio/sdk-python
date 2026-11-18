from temporalio import workflow


@workflow.defn
class MainModuleWorkflow:
    @workflow.run
    async def run(self) -> str:
        return "hello"
