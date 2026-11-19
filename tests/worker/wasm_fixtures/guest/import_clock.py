import time

import temporalio.workflow as workflow

time.time()


@workflow.defn
class ImportClock:
    @workflow.run
    async def run(self) -> None:
        pass
