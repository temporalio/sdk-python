import uuid

import temporalio.client


async def test_client_simple():
    client = await temporalio.client.Client.connect("http://localhost:7233")
    handle = await client.start_workflow(
        "my-workflow",
        "arg1",
        id=f"my-workflow-id-{uuid.uuid4}",
        task_queue=f"my-workflow-id-{uuid.uuid4}",
    )
    assert handle.run_id
    print(f"Workflow created with run ID: {handle.run_id}")
