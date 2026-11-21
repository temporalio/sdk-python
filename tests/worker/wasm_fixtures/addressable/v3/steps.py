from datetime import timedelta

import temporalio.workflow as workflow


async def score(value: int) -> int:
    if value < 0:
        return -value
    result = await workflow.execute_activity(
        "addressable_echo",
        value + 30,
        result_type=int,
        start_to_close_timeout=timedelta(seconds=10),
    )
    await workflow.sleep(timedelta(milliseconds=10))
    return result
