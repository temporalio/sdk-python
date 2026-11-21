from datetime import timedelta

import temporalio.workflow as workflow


async def score(value: int) -> int:
    return await workflow.execute_activity(
        "addressable_echo",
        value + 20,
        result_type=int,
        start_to_close_timeout=timedelta(seconds=10),
    )
