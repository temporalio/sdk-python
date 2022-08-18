from temporalio.testing import WorkflowEnvironment


async def test_temp1():
    await WorkflowEnvironment.start_time_skipping()
