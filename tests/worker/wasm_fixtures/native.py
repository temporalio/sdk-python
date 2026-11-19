import asyncio

import temporalio.workflow as workflow


@workflow.defn
class NativeWorkflow:
    @workflow.run
    async def run(self, value: str) -> str:
        await asyncio.sleep(0.01)
        return f"native:{value}"
