import uuid

import pytest

import temporalio.client
import temporalio.exceptions
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


async def test_start_with_signal(
    client: temporalio.client.Client, worker: utils.Worker
):
    handle = await client.start_workflow(
        "kitchen_sink",
        utils.KitchenSinkWorkflowParams(result_as_string_signal_arg="my-signal"),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
        start_signal="my-signal",
        start_signal_args=["some signal arg"],
    )
    assert "some signal arg" == await handle.result()


async def test_result_follow_continue_as_new(
    client: temporalio.client.Client, worker: utils.Worker
):
    handle = await client.start_workflow(
        "kitchen_sink",
        utils.KitchenSinkWorkflowParams(continue_as_new_count=1, result_as_run_id=True),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    final_run_id = await handle.result()
    assert len(final_run_id) > 5 and handle.run_id != final_run_id

    # Get a handle and check result without following and confirm
    # continue-as-new error
    with pytest.raises(temporalio.client.WorkflowContinuedAsNewError) as err:
        await handle.result(follow_runs=False)
    assert err.value.new_execution_run_id == final_run_id


async def test_workflow_failed(client: temporalio.client.Client, worker: utils.Worker):
    handle = await client.start_workflow(
        "kitchen_sink",
        utils.KitchenSinkWorkflowParams(
            error_with="some error", error_details={"foo": "bar", "baz": 123.45}
        ),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    with pytest.raises(temporalio.client.WorkflowFailureError) as err:
        await handle.result()
    assert isinstance(err.value.__cause__, temporalio.exceptions.ApplicationError)
    app_err = err.value.__cause__
    assert str(app_err) == "some error"
    assert app_err.details[0] == {"foo": "bar", "baz": 123.45}


async def test_cancel(client: temporalio.client.Client, worker: utils.Worker):
    handle = await client.start_workflow(
        "kitchen_sink",
        utils.KitchenSinkWorkflowParams(sleep_ms=50000),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    await handle.cancel()
    with pytest.raises(temporalio.client.WorkflowFailureError) as err:
        await handle.result()
    assert isinstance(err.value.__cause__, temporalio.exceptions.CancelledError)


async def test_result_terminated():
    # TODO(cretz): This
    pass


async def test_result_continued_as_new():
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
