import uuid

import pytest

import temporalio.client
from tests.fixtures import utils


async def test_start_id_reuse(client: temporalio.client.Client, worker: utils.Worker):
    # Run to return "some result"
    id = str(uuid.uuid4())
    handle = await client.start_workflow(
        "kitchen_sink",
        utils.KitchenSinkWorkflowParams(result="some result"),
        id=id,
        task_queue=worker.task_queue,
    )
    assert handle.run_id
    assert "some result" == await handle.result()

    # Run again with reject duplicate
    with pytest.raises(temporalio.client.RPCError) as err:
        handle = await client.start_workflow(
            "kitchen_sink",
            utils.KitchenSinkWorkflowParams(result="some result 2"),
            id=id,
            task_queue=worker.task_queue,
            id_reuse_policy=temporalio.client.WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
        await handle.result()
    assert err.value.status == temporalio.client.RPCStatusCode.ALREADY_EXISTS

    # Run again allowing duplicate (the default)
    handle = await client.start_workflow(
        "kitchen_sink",
        utils.KitchenSinkWorkflowParams(result="some result 3"),
        id=id,
        task_queue=worker.task_queue,
    )
    assert "some result 3" == await handle.result()


async def test_start_with_signal():
    # TODO(cretz): This
    pass


async def test_result():
    # TODO(cretz): This
    pass


async def test_result_follow_continue_as_new():
    # TODO(cretz): This
    pass


async def test_result_failed():
    # TODO(cretz): This
    pass


async def test_result_canceled():
    # TODO(cretz): This
    pass


async def test_result_terminated():
    # TODO(cretz): This
    pass


async def test_result_continued_as_new():
    # TODO(cretz): This
    pass


async def test_cancel():
    # TODO(cretz): This
    pass


async def test_cancel_not_found():
    # TODO(cretz): This
    pass


async def test_describe():
    # TODO(cretz): This
    pass


async def test_query():
    # TODO(cretz): This
    pass


async def test_query_rejected():
    # TODO(cretz): This
    pass


async def test_signal():
    # TODO(cretz): This
    pass


async def test_terminate():
    # TODO(cretz): This
    pass


async def test_interceptor():
    # TODO(cretz): This
    pass
