from temporalio import workflow

import tests.worker.workflow_sandbox.testmodules.lazy_module  # noqa: F401


@workflow.defn
class InitialImportWarningDependencyWorkflow:
    @workflow.run
    async def run(self) -> None:
        pass
