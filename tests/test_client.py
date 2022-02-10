import uuid
from typing import Any, List, Tuple

import pytest

import temporalio.api.enums.v1
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


async def test_terminate(client: temporalio.client.Client, worker: utils.Worker):
    handle = await client.start_workflow(
        "kitchen_sink",
        utils.KitchenSinkWorkflowParams(sleep_ms=50000),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    await handle.terminate("arg1", "arg2", reason="some reason")
    with pytest.raises(temporalio.client.WorkflowFailureError) as err:
        await handle.result()
    assert isinstance(err.value.__cause__, temporalio.exceptions.TerminatedError)
    assert str(err.value.__cause__) == "some reason"
    assert list(err.value.__cause__.details) == ["arg1", "arg2"]


async def test_cancel_not_found(client: temporalio.client.Client):
    with pytest.raises(temporalio.client.RPCError) as err:
        await client.get_workflow_handle("does-not-exist").cancel()
    assert err.value.status == temporalio.client.RPCStatusCode.NOT_FOUND


async def test_describe(client: temporalio.client.Client, worker: utils.Worker):
    handle = await client.start_workflow(
        "kitchen_sink",
        utils.KitchenSinkWorkflowParams(result="some value"),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    assert "some value" == await handle.result()
    desc = await handle.describe()
    assert desc.status == temporalio.client.WorkflowExecutionStatus.COMPLETED
    assert (
        desc.raw_message.workflow_execution_info.status
        == temporalio.api.enums.v1.WorkflowExecutionStatus.WORKFLOW_EXECUTION_STATUS_COMPLETED
    )


async def test_query(client: temporalio.client.Client, worker: utils.Worker):
    handle = await client.start_workflow(
        "kitchen_sink",
        utils.KitchenSinkWorkflowParams(queries_with_string_arg=["some query"]),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    await handle.result()
    assert "some query arg" == await handle.query("some query", "some query arg")
    # Try a query not on the workflow
    with pytest.raises(temporalio.client.RPCError) as err:
        await handle.query("does not exist")
    # TODO(cretz): Is this the status we expect all SDKs to report?
    assert err.value.status == temporalio.client.RPCStatusCode.INVALID_ARGUMENT


async def test_query_rejected(client: temporalio.client.Client, worker: utils.Worker):
    # Make a queryable workflow that waits on a signal
    handle = await client.start_workflow(
        "kitchen_sink",
        utils.KitchenSinkWorkflowParams(
            queries_with_string_arg=["some query"],
            result_as_string_signal_arg="some signal",
        ),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    # Confirm we can query w/ a not-open rejection condition since it's still
    # open
    assert "some query arg" == await handle.query(
        "some query",
        "some query arg",
        reject_condition=temporalio.client.WorkflowQueryRejectCondition.NOT_OPEN,
    )
    # But if we signal then wait for result, that same query should fail
    await handle.signal("some signal", "some signal arg")
    await handle.result()
    with pytest.raises(temporalio.client.WorkflowQueryRejectedError) as err:
        assert "some query arg" == await handle.query(
            "some query",
            "some query arg",
            reject_condition=temporalio.client.WorkflowQueryRejectCondition.NOT_OPEN,
        )
    assert err.value.status == temporalio.client.WorkflowExecutionStatus.COMPLETED


async def test_signal(client: temporalio.client.Client, worker: utils.Worker):
    handle = await client.start_workflow(
        "kitchen_sink",
        utils.KitchenSinkWorkflowParams(result_as_string_signal_arg="some signal"),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    await handle.signal("some signal", "some signal arg")
    assert "some signal arg" == await handle.result()


class TracingClientInterceptor(temporalio.client.Interceptor):
    def intercept_client(
        self, next: temporalio.client.OutboundInterceptor
    ) -> temporalio.client.OutboundInterceptor:
        self.traces: List[Tuple[str, Any]] = []
        return TracingClientOutboundInterceptor(self, next)


class TracingClientOutboundInterceptor(temporalio.client.OutboundInterceptor):
    def __init__(
        self,
        parent: TracingClientInterceptor,
        next: temporalio.client.OutboundInterceptor,
    ) -> None:
        super().__init__(next)
        self._parent = parent

    async def start_workflow(
        self, input: temporalio.client.StartWorkflowInput
    ) -> temporalio.client.WorkflowHandle[Any]:
        self._parent.traces.append(("start_workflow", input))
        return await super().start_workflow(input)

    async def cancel_workflow(
        self, input: temporalio.client.CancelWorkflowInput
    ) -> None:
        self._parent.traces.append(("cancel_workflow", input))
        return await super().cancel_workflow(input)

    async def query_workflow(self, input: temporalio.client.QueryWorkflowInput) -> Any:
        self._parent.traces.append(("query_workflow", input))
        return await super().query_workflow(input)

    async def signal_workflow(
        self, input: temporalio.client.SignalWorkflowInput
    ) -> None:
        self._parent.traces.append(("signal_workflow", input))
        return await super().signal_workflow(input)

    async def terminate_workflow(
        self, input: temporalio.client.TerminateWorkflowInput
    ) -> None:
        self._parent.traces.append(("terminate_workflow", input))
        return await super().terminate_workflow(input)


async def test_interceptor(client: temporalio.client.Client, worker: utils.Worker):
    # Create new client from existing client but with a tracing interceptor
    interceptor = TracingClientInterceptor()
    client = temporalio.client.Client(
        service=client.service, namespace=client.namespace, interceptors=[interceptor]
    )
    # Do things that would trigger the interceptors
    id = str(uuid.uuid4())
    handle = await client.start_workflow(
        "kitchen_sink",
        utils.KitchenSinkWorkflowParams(
            queries_with_string_arg=["some query"],
            result_as_string_signal_arg="some signal",
        ),
        id=id,
        task_queue=worker.task_queue,
    )
    await handle.query("some query", "some query arg")
    await handle.signal("some signal", "some signal arg")
    await handle.result()
    await handle.cancel()
    # Ignore this error
    with pytest.raises(temporalio.client.RPCError):
        await handle.terminate()

    # Check trace
    assert len(interceptor.traces) == 5
    assert interceptor.traces[0][0] == "start_workflow"
    assert interceptor.traces[0][1].workflow == "kitchen_sink"
    assert interceptor.traces[1][0] == "query_workflow"
    assert interceptor.traces[1][1].query == "some query"
    assert interceptor.traces[2][0] == "signal_workflow"
    assert interceptor.traces[2][1].signal == "some signal"
    assert interceptor.traces[3][0] == "cancel_workflow"
    assert interceptor.traces[3][1].id == id
    assert interceptor.traces[4][0] == "terminate_workflow"
    assert interceptor.traces[4][1].id == id


async def test_tls_config():
    # TODO(cretz): This
    pass
