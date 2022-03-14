import uuid
from datetime import timedelta
from typing import Any, List, Optional, Tuple

import pytest

import temporalio.api.enums.v1
import temporalio.api.workflowservice.v1
import temporalio.client
import temporalio.common
import temporalio.exceptions
from tests.helpers.worker import (
    KSAction,
    KSContinueAsNewAction,
    KSErrorAction,
    KSQueryHandlerAction,
    KSResultAction,
    KSSignalAction,
    KSSleepAction,
    KSWorkflowParams,
    Worker,
)


async def test_start_id_reuse(client: temporalio.client.Client, worker: Worker):
    # Run to return "some result"
    id = str(uuid.uuid4())
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(
            actions=[KSAction(result=KSResultAction(value="some result"))]
        ),
        id=id,
        task_queue=worker.task_queue,
    )
    assert "some result" == await handle.result()

    # Run again with reject duplicate
    with pytest.raises(temporalio.client.RPCError) as err:
        handle = await client.start_workflow(
            "kitchen_sink",
            KSWorkflowParams(
                actions=[KSAction(result=KSResultAction(value="some result 2"))]
            ),
            id=id,
            task_queue=worker.task_queue,
            id_reuse_policy=temporalio.common.WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
        await handle.result()
    assert err.value.status == temporalio.client.RPCStatusCode.ALREADY_EXISTS

    # Run again allowing duplicate (the default)
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(
            actions=[KSAction(result=KSResultAction(value="some result 3"))]
        ),
        id=id,
        task_queue=worker.task_queue,
    )
    assert "some result 3" == await handle.result()


async def test_start_with_signal(client: temporalio.client.Client, worker: Worker):
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(action_signal="my-signal"),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
        start_signal="my-signal",
        start_signal_args=[KSAction(result=KSResultAction(value="some signal arg"))],
    )
    assert "some signal arg" == await handle.result()


async def test_result_follow_continue_as_new(
    client: temporalio.client.Client, worker: Worker
):
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(
            actions=[
                KSAction(continue_as_new=KSContinueAsNewAction(while_above_zero=1)),
                KSAction(result=KSResultAction(run_id=True)),
            ],
        ),
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


async def test_workflow_failed(client: temporalio.client.Client, worker: Worker):
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(
            actions=[
                KSAction(
                    error=KSErrorAction(
                        message="some error", details={"foo": "bar", "baz": 123.45}
                    )
                )
            ],
        ),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    with pytest.raises(temporalio.client.WorkflowFailureError) as err:
        await handle.result()
    assert isinstance(err.value.cause, temporalio.exceptions.ApplicationError)
    assert str(err.value.cause) == "some error"
    assert err.value.cause.details[0] == {"foo": "bar", "baz": 123.45}


async def test_cancel(client: temporalio.client.Client, worker: Worker):
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(actions=[KSAction(sleep=KSSleepAction(millis=50000))]),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    await handle.cancel()
    with pytest.raises(temporalio.client.WorkflowFailureError) as err:
        await handle.result()
    assert isinstance(err.value.cause, temporalio.exceptions.CancelledError)


async def test_terminate(client: temporalio.client.Client, worker: Worker):
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(actions=[KSAction(sleep=KSSleepAction(millis=50000))]),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    await handle.terminate("arg1", "arg2", reason="some reason")
    with pytest.raises(temporalio.client.WorkflowFailureError) as err:
        await handle.result()
    assert isinstance(err.value.cause, temporalio.exceptions.TerminatedError)
    assert str(err.value.cause) == "some reason"
    assert list(err.value.cause.details) == ["arg1", "arg2"]


async def test_cancel_not_found(client: temporalio.client.Client):
    with pytest.raises(temporalio.client.RPCError) as err:
        await client.get_workflow_handle("does-not-exist").cancel()
    assert err.value.status == temporalio.client.RPCStatusCode.NOT_FOUND


async def test_describe(client: temporalio.client.Client, worker: Worker):
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(actions=[KSAction(result=KSResultAction(value="some value"))]),
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


async def test_query(client: temporalio.client.Client, worker: Worker):
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(
            actions=[KSAction(query_handler=KSQueryHandlerAction(name="some query"))]
        ),
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


async def test_query_rejected(client: temporalio.client.Client, worker: Worker):
    # Make a queryable workflow that waits on a signal
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(
            actions=[
                KSAction(query_handler=KSQueryHandlerAction(name="some query")),
                KSAction(signal=KSSignalAction(name="some signal")),
            ],
        ),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    # Confirm we can query w/ a not-open rejection condition since it's still
    # open
    assert "some query arg" == await handle.query(
        "some query",
        "some query arg",
        reject_condition=temporalio.common.QueryRejectCondition.NOT_OPEN,
    )
    # But if we signal then wait for result, that same query should fail
    await handle.signal("some signal", "some signal arg")
    await handle.result()
    with pytest.raises(temporalio.client.WorkflowQueryRejectedError) as err:
        assert "some query arg" == await handle.query(
            "some query",
            "some query arg",
            reject_condition=temporalio.common.QueryRejectCondition.NOT_OPEN,
        )
    assert err.value.status == temporalio.client.WorkflowExecutionStatus.COMPLETED


async def test_signal(client: temporalio.client.Client, worker: Worker):
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(action_signal="some signal"),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    await handle.signal(
        "some signal",
        KSAction(result=KSResultAction(value="some signal arg")),
    )
    assert "some signal arg" == await handle.result()


async def test_retry_policy(client: temporalio.client.Client, worker: Worker):
    # Make the workflow retry 3 times w/ no real backoff
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(actions=[KSAction(error=KSErrorAction(attempt=True))]),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
        retry_policy=temporalio.common.RetryPolicy(
            initial_interval=timedelta(milliseconds=1),
            maximum_attempts=3,
        ),
    )
    with pytest.raises(temporalio.client.WorkflowFailureError) as err:
        await handle.result()
    assert isinstance(err.value.cause, temporalio.exceptions.ApplicationError)
    assert str(err.value.cause) == "attempt 3"


async def test_single_client_config_change(
    client: temporalio.client.Client, worker: Worker
):
    # Make sure normal query works on completed workflow
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(
            actions=[KSAction(query_handler=KSQueryHandlerAction(name="some query"))]
        ),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    await handle.result()
    assert "some query arg" == await handle.query("some query", "some query arg")
    # Now create a client with the rejection condition changed to not open
    config = client.config()
    config[
        "default_workflow_query_reject_condition"
    ] = temporalio.common.QueryRejectCondition.NOT_OPEN
    reject_client = temporalio.client.Client(**config)
    with pytest.raises(temporalio.client.WorkflowQueryRejectedError):
        await reject_client.get_workflow_handle(handle.id).query(
            "some query", "some query arg"
        )


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


async def test_interceptor(client: temporalio.client.Client, worker: Worker):
    # Create new client from existing client but with a tracing interceptor
    interceptor = TracingClientInterceptor()
    config = client.config()
    config["interceptors"] = [interceptor]
    client = temporalio.client.Client(**config)
    # Do things that would trigger the interceptors
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(
            actions=[
                KSAction(query_handler=KSQueryHandlerAction(name="some query")),
                KSAction(signal=KSSignalAction(name="some signal")),
            ],
        ),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    await handle.query("some query", "some query arg")
    await handle.signal("some signal")
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
    assert interceptor.traces[3][1].id == handle.id
    assert interceptor.traces[4][0] == "terminate_workflow"
    assert interceptor.traces[4][1].id == handle.id


async def test_interceptor_callable(client: temporalio.client.Client, worker: Worker):
    # Create new client from existing client but with a tracing interceptor
    # callable and only check a simple call
    interceptor = TracingClientInterceptor()
    config = client.config()
    config["interceptors"] = [interceptor.intercept_client]
    client = temporalio.client.Client(**config)
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    await handle.result()

    # Check trace
    assert interceptor.traces[0][0] == "start_workflow"
    assert interceptor.traces[0][1].workflow == "kitchen_sink"


async def test_tls_config(tls_client: Optional[temporalio.client.Client]):
    if not tls_client:
        pytest.skip("No TLS client")
    resp = await tls_client.service.describe_namespace(
        temporalio.api.workflowservice.v1.DescribeNamespaceRequest(
            namespace=tls_client.namespace
        )
    )
    assert resp.namespace_info.name == tls_client.namespace
