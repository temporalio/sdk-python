"""Tests for @nexus.completion_operation and Nexus completion task handling."""

import asyncio
from dataclasses import dataclass
from typing import Any

import nexusrpc
import pytest

import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.api.nexus.v1
import temporalio.common
from temporalio import nexus
from temporalio.client import (
    Client,
    Interceptor,
    OutboundInterceptor,
    StartWorkflowInput,
    WorkflowHandle,
)
from temporalio.exceptions import ApplicationError
from temporalio.nexus._link_conversion import workflow_event_to_nexus_link
from temporalio.nexus._util import get_completion_handler_definition
from temporalio.worker._nexus import (
    _collect_completion_handlers,
    _NexusTaskCancellation,
    _NexusWorker,
)


@dataclass
class Result:
    value: str


# -----------------------------------------------------------------------------
# Decorator tests


def test_completion_operation_sets_definition() -> None:
    class Handlers:
        @nexus.completion_operation
        async def on_done(
            self,
            ctx: nexus.TemporalCompletionContext,
            client: nexus.TemporalCompletionClient,
            completion: nexus.Completion[Result],
        ) -> None: ...

        @nexus.completion_operation(name="custom-name")
        async def renamed(
            self,
            ctx: nexus.TemporalCompletionContext,
            client: nexus.TemporalCompletionClient,
            completion: nexus.NexusOperationCompletion[str],
        ) -> None: ...

    defn = get_completion_handler_definition(Handlers.on_done)
    assert defn
    assert defn.name == "on_done"
    assert defn.method_name == "on_done"
    assert defn.result_type is Result

    renamed_defn = get_completion_handler_definition(Handlers.renamed)
    assert renamed_defn
    assert renamed_defn.name == "custom-name"
    assert renamed_defn.method_name == "renamed"
    assert renamed_defn.result_type is str


def test_completion_operation_requires_async_method() -> None:
    with pytest.raises(RuntimeError, match="async def"):

        class Handlers:
            @nexus.completion_operation  # type: ignore
            def on_done(
                self,
                ctx: nexus.TemporalCompletionContext,
                client: nexus.TemporalCompletionClient,
                completion: nexus.Completion[Result],
            ) -> None: ...


def test_completion_operation_warns_on_bad_completion_annotation() -> None:
    with pytest.warns(UserWarning, match="annotated as a subclass of Completion"):

        class Handlers:
            @nexus.completion_operation  # type: ignore
            async def on_done(
                self,
                ctx: nexus.TemporalCompletionContext,
                client: nexus.TemporalCompletionClient,
                completion: list[str],
            ) -> None: ...

    defn = get_completion_handler_definition(Handlers.on_done)
    assert defn
    assert defn.result_type is None


def test_completion_operation_allows_unparameterized_completion() -> None:
    class Handlers:
        @nexus.completion_operation
        async def on_done(
            self,
            ctx: nexus.TemporalCompletionContext,
            client: nexus.TemporalCompletionClient,
            completion: nexus.Completion,
        ) -> None: ...

    defn = get_completion_handler_definition(Handlers.on_done)
    assert defn
    assert defn.result_type is None


# -----------------------------------------------------------------------------
# Registry tests


@nexusrpc.handler.service_handler
class ServiceA:
    @nexus.completion_operation
    async def on_done(
        self,
        ctx: nexus.TemporalCompletionContext,
        client: nexus.TemporalCompletionClient,
        completion: nexus.Completion[Result],
    ) -> None: ...


def test_collect_completion_handlers() -> None:
    handlers = _collect_completion_handlers([ServiceA()])
    assert ("ServiceA", "on_done") in handlers
    assert handlers[("ServiceA", "on_done")].result_type is Result


def test_collect_completion_handlers_rejects_duplicates() -> None:
    with pytest.raises(ValueError, match="Multiple completion handlers"):
        _collect_completion_handlers([ServiceA(), ServiceA()])


# -----------------------------------------------------------------------------
# Worker completion handling tests


class _LinkCapturingInterceptor(Interceptor):
    def __init__(self) -> None:
        self.start_workflow_inputs: list[StartWorkflowInput] = []

    def intercept_client(self, next: OutboundInterceptor) -> OutboundInterceptor:
        captured = self

        class _Outbound(OutboundInterceptor):
            async def start_workflow(
                self, input: StartWorkflowInput
            ) -> WorkflowHandle[Any, Any]:
                captured.start_workflow_inputs.append(input)
                return await self.next.start_workflow(input)

        return _Outbound(next)


def _make_worker(client: Client, service_handlers: list[Any]) -> _NexusWorker:
    return _NexusWorker(
        bridge_worker=lambda: None,  # type: ignore[arg-type,return-value]
        client=client,
        namespace=client.namespace,
        task_queue="completion-test-task-queue",
        service_handlers=service_handlers,
        data_converter=client.data_converter,
        interceptors=[],
        metric_meter=temporalio.common.MetricMeter.noop,
        executor=None,
    )


def _workflow_event_link() -> temporalio.api.nexus.v1.Link:
    nexus_link = workflow_event_to_nexus_link(
        temporalio.api.common.v1.Link.WorkflowEvent(
            namespace="ns",
            workflow_id="wid",
            run_id="rid",
            event_ref=temporalio.api.common.v1.Link.WorkflowEvent.EventReference(
                event_id=1,
                event_type=temporalio.api.enums.v1.EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED,
            ),
        )
    )
    return temporalio.api.nexus.v1.Link(url=nexus_link.url, type=nexus_link.type)


async def _completion_request(
    client: Client,
    *,
    service: str,
    operation: str,
    result: Optional[Any] = None,
    failure: Optional[BaseException] = None,
    links: list[temporalio.api.nexus.v1.Link] = [],
) -> temporalio.api.nexus.v1.CompletionRequest:
    nexus_operation = (
        temporalio.api.nexus.v1.CompletionRequest.NexusOperationCompletion(
            service="caller-side-service",
            operation="caller-side-operation",
            operation_id="op-id",
            operation_token="op-token",
            links=links,
        )
    )
    if failure is not None:
        await client.data_converter.encode_failure(failure, nexus_operation.failure)
    elif result is not None:
        [payload] = await client.data_converter.encode([result])
        nexus_operation.result.CopyFrom(payload)
    return temporalio.api.nexus.v1.CompletionRequest(
        service=service,
        operation=operation,
        request_id="req-id",
        nexus_operation=nexus_operation,
    )


async def _handle_completion(
    worker: _NexusWorker,
    request: temporalio.api.nexus.v1.CompletionRequest,
) -> temporalio.api.nexus.v1.CompletionResponse:
    return await worker._handle_completion(
        request,
        headers={},
        cancellation=_NexusTaskCancellation(),
        request_deadline=None,
        endpoint="test-endpoint",
    )


async def test_completion_handler_success(client: Client) -> None:
    received: list[nexus.Completion[Result]] = []

    @nexusrpc.handler.service_handler
    class Handlers:
        @nexus.completion_operation
        async def on_done(
            self,
            ctx: nexus.TemporalCompletionContext,
            client: nexus.TemporalCompletionClient,
            completion: nexus.Completion[Result],
        ) -> None:
            assert ctx.service == "Handlers"
            assert ctx.operation == "on_done"
            assert ctx.request_id == "req-id"
            assert nexus.in_operation()
            assert nexus.info().task_queue == "completion-test-task-queue"
            received.append(completion)

    worker = _make_worker(client, [Handlers()])
    request = await _completion_request(
        client,
        service="Handlers",
        operation="on_done",
        result=Result(value="hello"),
    )
    response = await _handle_completion(worker, request)
    assert not response.HasField("failure")

    [completion] = received
    assert isinstance(completion, nexus.NexusOperationCompletion)
    assert completion.result == Result(value="hello")
    assert completion.failure is None
    assert completion.service == "caller-side-service"
    assert completion.operation == "caller-side-operation"
    assert completion.operation_id == "op-id"
    assert completion.operation_token == "op-token"


async def test_completion_handler_receives_decoded_failure(client: Client) -> None:
    received: list[nexus.Completion[Result]] = []

    @nexusrpc.handler.service_handler
    class Handlers:
        @nexus.completion_operation
        async def on_done(
            self,
            ctx: nexus.TemporalCompletionContext,
            client: nexus.TemporalCompletionClient,
            completion: nexus.Completion[Result],
        ) -> None:
            received.append(completion)

    worker = _make_worker(client, [Handlers()])
    request = await _completion_request(
        client,
        service="Handlers",
        operation="on_done",
        failure=ApplicationError("operation failed", type="MyError"),
    )
    response = await _handle_completion(worker, request)
    assert not response.HasField("failure")

    [completion] = received
    assert completion.result is None
    assert isinstance(completion.failure, ApplicationError)
    assert completion.failure.message == "operation failed"


async def test_completion_handler_not_found(client: Client) -> None:
    @nexusrpc.handler.service_handler
    class Handlers:
        @nexus.completion_operation
        async def on_done(
            self,
            ctx: nexus.TemporalCompletionContext,
            client: nexus.TemporalCompletionClient,
            completion: nexus.Completion[Result],
        ) -> None: ...

    worker = _make_worker(client, [Handlers()])
    request = await _completion_request(
        client, service="Handlers", operation="nonexistent"
    )
    with pytest.raises(nexusrpc.HandlerError) as err:
        await _handle_completion(worker, request)
    assert err.value.type == nexusrpc.HandlerErrorType.NOT_FOUND


async def test_completion_handler_unset_variant(client: Client) -> None:
    @nexusrpc.handler.service_handler
    class Handlers:
        @nexus.completion_operation
        async def on_done(
            self,
            ctx: nexus.TemporalCompletionContext,
            client: nexus.TemporalCompletionClient,
            completion: nexus.Completion[Result],
        ) -> None: ...

    worker = _make_worker(client, [Handlers()])
    request = temporalio.api.nexus.v1.CompletionRequest(
        service="Handlers", operation="on_done", request_id="req-id"
    )
    with pytest.raises(nexusrpc.HandlerError) as err:
        await _handle_completion(worker, request)
    assert err.value.type == nexusrpc.HandlerErrorType.NOT_IMPLEMENTED


async def test_completion_handler_terminal_failure(client: Client) -> None:
    @nexusrpc.handler.service_handler
    class Handlers:
        @nexus.completion_operation
        async def on_done(
            self,
            ctx: nexus.TemporalCompletionContext,
            client: nexus.TemporalCompletionClient,
            completion: nexus.Completion[Result],
        ) -> None:
            raise ApplicationError("cannot process", non_retryable=True)

    worker = _make_worker(client, [Handlers()])
    request = await _completion_request(
        client, service="Handlers", operation="on_done", result=Result(value="x")
    )
    response = await _handle_completion(worker, request)
    assert response.HasField("failure")
    assert response.failure.message == "cannot process"


async def test_completion_handler_operation_error_is_terminal(client: Client) -> None:
    @nexusrpc.handler.service_handler
    class Handlers:
        @nexus.completion_operation
        async def on_done(
            self,
            ctx: nexus.TemporalCompletionContext,
            client: nexus.TemporalCompletionClient,
            completion: nexus.Completion[Result],
        ) -> None:
            raise nexusrpc.OperationError(
                "cannot process", state=nexusrpc.OperationErrorState.FAILED
            )

    worker = _make_worker(client, [Handlers()])
    request = await _completion_request(
        client, service="Handlers", operation="on_done", result=Result(value="x")
    )
    response = await _handle_completion(worker, request)
    assert response.HasField("failure")
    assert response.failure.message == "cannot process"


async def test_completion_handler_retryable_error_propagates(client: Client) -> None:
    @nexusrpc.handler.service_handler
    class Handlers:
        @nexus.completion_operation
        async def on_done(
            self,
            ctx: nexus.TemporalCompletionContext,
            client: nexus.TemporalCompletionClient,
            completion: nexus.Completion[Result],
        ) -> None:
            raise RuntimeError("transient")

    worker = _make_worker(client, [Handlers()])
    request = await _completion_request(
        client, service="Handlers", operation="on_done", result=Result(value="x")
    )
    with pytest.raises(RuntimeError, match="transient"):
        await _handle_completion(worker, request)


async def test_completion_client_attaches_links_to_started_workflow(
    client: Client,
) -> None:
    interceptor = _LinkCapturingInterceptor()
    config = client.config()
    config["interceptors"] = [interceptor]
    intercepted_client = Client(**config)

    started: asyncio.Event = asyncio.Event()

    @nexusrpc.handler.service_handler
    class Handlers:
        @nexus.completion_operation
        async def on_done(
            self,
            ctx: nexus.TemporalCompletionContext,
            client: nexus.TemporalCompletionClient,
            completion: nexus.Completion[Result],
        ) -> None:
            assert completion.result is not None
            try:
                await client.start_workflow(
                    "some-workflow",
                    completion.result.value,
                    id="completion-link-test",
                )
            except Exception:
                # The workflow type is not registered on any worker; we only care
                # that the start request carried the links.
                pass
            started.set()

    worker = _make_worker(intercepted_client, [Handlers()])
    request = await _completion_request(
        intercepted_client,
        service="Handlers",
        operation="on_done",
        result=Result(value="x"),
        links=[_workflow_event_link()],
    )
    response = await _handle_completion(worker, request)
    assert not response.HasField("failure")
    assert started.is_set()

    [input] = interceptor.start_workflow_inputs
    assert input.task_queue == "completion-test-task-queue"
    [link] = input.links
    assert link.workflow_event.namespace == "ns"
    assert link.workflow_event.workflow_id == "wid"
    assert link.workflow_event.run_id == "rid"
