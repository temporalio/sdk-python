"""
File used in conjunction with external_stack_trace.py to test filenames in multi-file workflows.
"""

from asyncio import sleep

from temporalio import workflow


async def never_completing_coroutine(status) -> None:
    status[0] = "waiting"  # external coroutine test
    await workflow.wait_condition(lambda: False)


async def wait_on_timer(status) -> None:
    status[0] = "waiting"  # multifile test
    print("Coroutine executed, waiting.")
    await workflow.wait_condition(lambda: False)
