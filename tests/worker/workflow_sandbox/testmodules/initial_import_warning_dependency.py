import tests.worker.workflow_sandbox.testmodules.lazy_module  # noqa: F401
from temporalio import workflow


@workflow.defn
class InitialImportWarningDependencyWorkflow:
    @workflow.run
    async def run(self) -> None:
        pass
