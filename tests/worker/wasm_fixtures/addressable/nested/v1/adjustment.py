from datetime import timedelta

import temporalio.workflow as workflow


async def adjust(value: int) -> int:
    await workflow.sleep(timedelta(seconds=5))
    return value + 10
