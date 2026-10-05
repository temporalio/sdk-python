"""Tests for Nexus handler completion callbacks."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import nexusrpc
import nexusrpc.handler
import pytest
from nexusrpc.handler import service_handler

import temporalio.api.common.v1
import temporalio.converter
import temporalio.nexus.notifications as notifications
from temporalio import activity, nexus, workflow
from temporalio.client import (
    Client,
    NexusOperationFailureError,
    WorkflowUpdateStage,
)
from temporalio.common import RawValue
from temporalio.converter import PayloadCodec
from temporalio.exceptions import ApplicationError, CancelledError, TerminatedError
from temporalio.nexus import WorkflowRunOperationContext, workflow_run_operation
from temporalio.nexus._notification import _OnCompleteCallback
from temporalio.nexus.notifications.models import (
    OnCompleteRequestResultFailure,
    OnCompleteRequestResultSuccess,
)
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers import assert_eventually
from tests.helpers.nexus import make_nexus_endpoint_name
from tests.nexus.test_standalone_operations import (
    BlockingHandlerWorkflow,
    EchoHandlerWorkflow,
    EchoInput,
    EchoOutput,
    StandaloneTestService,
    StandaloneTestServiceHandler,
    _RecordingInterceptor,
)


@dataclasses.dataclass
class NotificationValue:
    message: str


class PrefixCodec(PayloadCodec):
    """Reversible codec that rejects payloads it did not encode."""

    async def encode(
        self, payloads: Sequence[temporalio.api.common.v1.Payload]
    ) -> list[temporalio.api.common.v1.Payload]:
        return [
            temporalio.api.common.v1.Payload(
                metadata={"encoding": b"test/prefix"},
                data=b"prefix:" + payload.SerializeToString(),
            )
            for payload in payloads
        ]

    async def decode(
        self, payloads: Sequence[temporalio.api.common.v1.Payload]
    ) -> list[temporalio.api.common.v1.Payload]:
        decoded: list[temporalio.api.common.v1.Payload] = []
        for payload in payloads:
            if payload.metadata.get("encoding") != b"test/prefix":
                raise RuntimeError(
                    f"unexpected payload passed to codec: {dict(payload.metadata)}"
                )
            decoded.append(
                temporalio.api.common.v1.Payload.FromString(
                    payload.data.removeprefix(b"prefix:")
                )
            )
        return decoded


# ---------------------------------------------------------------------------
# Creating completion callbacks
# ---------------------------------------------------------------------------


@nexusrpc.handler.service_handler(name="custom.notification.service")
class CustomNotificationHandler:
    @nexusrpc.handler.sync_operation(name="CustomOnComplete")
    async def on_complete(
        self,
        _ctx: nexusrpc.handler.StartOperationContext,
        _input: notifications.OnCompleteRequest[NotificationValue, NotificationValue],
    ) -> notifications.OnCompleteResponse:
        return notifications.OnCompleteResponse()


@nexusrpc.handler.service_handler(name="sub.notification.service")
class SubNotificationHandler(CustomNotificationHandler):
    pass


@nexusrpc.service(name="renamed.notification.service")
class RenamedNotificationService:
    on_complete: nexusrpc.Operation[
        notifications.OnCompleteRequest[NotificationValue, NotificationValue],
        notifications.OnCompleteResponse,
    ] = nexusrpc.Operation(name="RenamedOnComplete")


@nexusrpc.service(name="sub.renamed.notification.service")
class SubRenamedNotificationService(RenamedNotificationService):
    pass


@nexusrpc.handler.service_handler(service=RenamedNotificationService)
class RenamedNotificationHandler:
    @nexusrpc.handler.sync_operation
    async def on_complete(
        self,
        _ctx: nexusrpc.handler.StartOperationContext,
        _input: notifications.OnCompleteRequest[NotificationValue, NotificationValue],
    ) -> notifications.OnCompleteResponse:
        return notifications.OnCompleteResponse()


@nexusrpc.handler.service_handler(name="shape.notification.service")
class NotificationShapeHandler:
    @nexusrpc.handler.sync_operation(name="BareOnComplete")
    async def bare_on_complete(
        self,
        _ctx: nexusrpc.handler.StartOperationContext,
        _input: notifications.OnCompleteRequest,
    ) -> notifications.OnCompleteResponse:
        return notifications.OnCompleteResponse()

    @nexusrpc.handler.sync_operation(name="WrongOutput")
    async def wrong_output(
        self,
        _ctx: nexusrpc.handler.StartOperationContext,
        _input: notifications.OnCompleteRequest[NotificationValue, NotificationValue],
    ) -> str:
        return ""


@nexusrpc.handler.service_handler(name="kinds.notification.service")
class OperationKindsNotificationHandler:
    @nexusrpc.handler.sync_operation(name="DefOnComplete")
    def def_on_complete(
        self,
        _ctx: nexusrpc.handler.StartOperationContext,
        _input: notifications.OnCompleteRequest[NotificationValue, NotificationValue],
    ) -> notifications.OnCompleteResponse:
        return notifications.OnCompleteResponse()

    @workflow_run_operation(name="WorkflowRunOnComplete")
    async def workflow_run_on_complete(
        self,
        _ctx: WorkflowRunOperationContext,
        _input: notifications.OnCompleteRequest[NotificationValue, NotificationValue],
    ) -> nexus.WorkflowHandle[notifications.OnCompleteResponse]:
        raise NotImplementedError

    @nexus.temporal_operation(name="TemporalOnComplete")
    async def temporal_on_complete(
        self,
        _ctx: nexus.TemporalStartOperationContext,
        _client: nexus.TemporalNexusClient,
        _input: notifications.OnCompleteRequest[NotificationValue, NotificationValue],
    ) -> nexus.TemporalOperationResult[notifications.OnCompleteResponse]:
        raise NotImplementedError


@nexusrpc.handler.service_handler(name="not-a-notification-service")
class OtherOperationHandler:
    @nexusrpc.handler.sync_operation(name="OtherOperation")
    async def other_operation(
        self,
        _ctx: nexusrpc.handler.StartOperationContext,
        input: str,
    ) -> str:
        return input


def test_completion_callback_handler_method() -> None:
    """A handler method resolves to its service and registered operation name."""
    callback = nexus.create_completion_callback(
        operation=CustomNotificationHandler.on_complete,
        task_queue="notifications",
        source_context=NotificationValue("context"),
    )
    assert callback == _OnCompleteCallback(
        task_queue="notifications",
        source_context=NotificationValue("context"),
        service="custom.notification.service",
        operation="CustomOnComplete",
    )


def test_completion_callback_operation_kinds() -> None:
    """def sync_operation, workflow_run_operation, and temporal_operation methods are supported."""
    for operation, name in (
        (OperationKindsNotificationHandler.def_on_complete, "DefOnComplete"),
        (
            OperationKindsNotificationHandler.workflow_run_on_complete,
            "WorkflowRunOnComplete",
        ),
        (OperationKindsNotificationHandler.temporal_on_complete, "TemporalOnComplete"),
    ):
        callback = nexus.create_completion_callback(
            operation=operation,
            task_queue="notifications",
            source_context=NotificationValue("context"),
        )
        assert callback == _OnCompleteCallback(
            task_queue="notifications",
            source_context=NotificationValue("context"),
            service="kinds.notification.service",
            operation=name,
        )


def test_completion_callback_renamed_by_service_definition() -> None:
    """Definition operations and handler methods use the definition's operation name."""
    for operation in (
        RenamedNotificationService.on_complete,
        RenamedNotificationHandler.on_complete,
    ):
        callback = nexus.create_completion_callback(
            operation=operation,
            task_queue="notifications",
            source_context=NotificationValue("context"),
        )
        assert callback == _OnCompleteCallback(
            task_queue="notifications",
            source_context=NotificationValue("context"),
            service="renamed.notification.service",
            operation="RenamedOnComplete",
        )


def test_completion_callback_inherited_operations() -> None:
    """Inherited operations resolve to the service they are accessed on."""
    handler_callback = nexus.create_completion_callback(
        operation=SubNotificationHandler.on_complete,
        task_queue="notifications",
        source_context=NotificationValue("context"),
    )
    assert handler_callback == _OnCompleteCallback(
        task_queue="notifications",
        source_context=NotificationValue("context"),
        service="sub.notification.service",
        operation="CustomOnComplete",
    )
    definition_callback = nexus.create_completion_callback(
        operation=SubRenamedNotificationService.on_complete,
        task_queue="notifications",
        source_context=NotificationValue("context"),
    )
    assert definition_callback == _OnCompleteCallback(
        task_queue="notifications",
        source_context=NotificationValue("context"),
        service="sub.renamed.notification.service",
        operation="RenamedOnComplete",
    )


def test_completion_callback_local_handler_class() -> None:
    """Handler classes defined in a function are supported."""

    @nexusrpc.handler.service_handler(name="local.notification.service")
    class LocalNotificationHandler:
        @nexusrpc.handler.sync_operation(name="OnComplete")
        async def on_complete(
            self,
            _ctx: nexusrpc.handler.StartOperationContext,
            _input: notifications.OnCompleteRequest[
                NotificationValue, NotificationValue
            ],
        ) -> notifications.OnCompleteResponse:
            return notifications.OnCompleteResponse()

    callback = nexus.create_completion_callback(
        operation=LocalNotificationHandler.on_complete,
        task_queue="notifications",
        source_context=NotificationValue("context"),
    )
    assert callback == _OnCompleteCallback(
        task_queue="notifications",
        source_context=NotificationValue("context"),
        service="local.notification.service",
        operation="OnComplete",
    )


def test_completion_callback_names() -> None:
    """A service name is given with an operation name, and only with names."""
    assert nexus.create_completion_callback(
        service="external.notification.service",
        operation="ExternalOnComplete",
        task_queue="notifications",
        source_context=NotificationValue("context"),
    ) == _OnCompleteCallback(
        task_queue="notifications",
        source_context=NotificationValue("context"),
        service="external.notification.service",
        operation="ExternalOnComplete",
    )
    invalid_names = "^Give a service name with an operation name"
    with pytest.raises(ValueError, match=invalid_names):
        nexus.create_completion_callback(  # type: ignore[call-overload]
            operation="ExternalOnComplete",  # pyright: ignore[reportArgumentType]
            task_queue="notifications",
            source_context=NotificationValue("context"),
        )
    with pytest.raises(ValueError, match=invalid_names):
        nexus.create_completion_callback(  # type: ignore[call-overload]
            service="custom.notification.service",
            operation=CustomNotificationHandler.on_complete,  # pyright: ignore[reportArgumentType]
            task_queue="notifications",
            source_context=NotificationValue("context"),
        )
    for service, operation in (("", "ExternalOnComplete"), ("external", "")):
        with pytest.raises(
            ValueError, match="^Service and operation names must not be empty$"
        ):
            nexus.create_completion_callback(
                service=service,
                operation=operation,
                task_queue="notifications",
                source_context=NotificationValue("context"),
            )


def test_completion_callback_rejects_invalid_operations() -> None:
    """Operations that are not on-complete operations of a service are rejected."""

    class UndecoratedHandler:
        async def on_complete(
            self,
            _ctx: nexusrpc.handler.StartOperationContext,
            _input: notifications.OnCompleteRequest[
                NotificationValue, NotificationValue
            ],
        ) -> notifications.OnCompleteResponse:
            return notifications.OnCompleteResponse()

    class UndecoratedOperationHandler:
        @nexusrpc.handler.sync_operation(name="OnComplete")
        async def on_complete(
            self,
            _ctx: nexusrpc.handler.StartOperationContext,
            _input: notifications.OnCompleteRequest[
                NotificationValue, NotificationValue
            ],
        ) -> notifications.OnCompleteResponse:
            return notifications.OnCompleteResponse()

    with pytest.raises(
        ValueError, match="^.*UndecoratedHandler.on_complete is not a Nexus operation$"
    ):
        nexus.create_completion_callback(
            operation=UndecoratedHandler.on_complete,
            task_queue="notifications",
            source_context=NotificationValue("context"),
        )
    with pytest.raises(
        ValueError,
        match=(
            "^.*UndecoratedOperationHandler.on_complete is not an operation of a "
            "Nexus service"
        ),
    ):
        nexus.create_completion_callback(
            operation=UndecoratedOperationHandler.on_complete,
            task_queue="notifications",
            source_context=NotificationValue("context"),
        )
    with pytest.raises(ValueError, match="^operation 'Free' is not an operation"):
        nexus.create_completion_callback(
            operation=nexusrpc.Operation[
                notifications.OnCompleteRequest[NotificationValue, NotificationValue],
                notifications.OnCompleteResponse,
            ](name="Free"),
            task_queue="notifications",
            source_context=NotificationValue("context"),
        )
    with pytest.raises(
        ValueError,
        match=(
            "^Operation 'OtherOperation' of service 'not-a-notification-service' is "
            "not an on-complete operation: expected input OnCompleteRequest and "
            "output OnCompleteResponse, got input <class 'str'> and output "
            "<class 'str'>$"
        ),
    ):
        nexus.create_completion_callback(
            operation=OtherOperationHandler.other_operation,  # type: ignore[arg-type]
            task_queue="notifications",
            source_context=NotificationValue("context"),
        )


def test_completion_callback_checks_operation_shape() -> None:
    """Operations need an OnCompleteRequest input and an OnCompleteResponse output."""
    assert nexus.create_completion_callback(
        operation=NotificationShapeHandler.bare_on_complete,
        task_queue="notifications",
        source_context=NotificationValue("context"),
    ) == _OnCompleteCallback(
        task_queue="notifications",
        source_context=NotificationValue("context"),
        service="shape.notification.service",
        operation="BareOnComplete",
    )
    with pytest.raises(
        ValueError,
        match="'WrongOutput' .* is not an on-complete operation: .* output <class 'str'>$",
    ):
        nexus.create_completion_callback(
            operation=NotificationShapeHandler.wrong_output,
            task_queue="notifications",
            source_context=NotificationValue("context"),
        )


async def test_on_complete_callback_to_proto() -> None:
    """The callback proto targets the handler operation with an encoded context."""
    callback = nexus.create_completion_callback(
        operation=CustomNotificationHandler.on_complete,
        task_queue="notifications",
        source_context=NotificationValue("context"),
    )
    proto = await callback._to_proto(temporalio.converter.DataConverter.default)

    assert proto.nexus_handler.service == "custom.notification.service"
    assert proto.nexus_handler.operation == "CustomOnComplete"
    assert proto.nexus_handler.task_queue_name == "notifications"
    assert await temporalio.converter.DataConverter.default.decode(
        [proto.nexus_handler.source_context], [NotificationValue]
    ) == [NotificationValue("context")]


async def test_on_complete_callback_to_proto_applies_codec() -> None:
    """The source context is encoded with the full data converter, including codecs."""
    data_converter = dataclasses.replace(
        temporalio.converter.default(), payload_codec=PrefixCodec()
    )

    callback = nexus.create_completion_callback(
        operation=CustomNotificationHandler.on_complete,
        task_queue="notifications",
        source_context=NotificationValue("context"),
    )
    proto = await callback._to_proto(data_converter)

    source_context = proto.nexus_handler.source_context
    assert source_context.metadata["encoding"] == b"test/prefix"
    assert await data_converter.decode([source_context], [NotificationValue]) == [
        NotificationValue("context")
    ]


# ---------------------------------------------------------------------------
# Delivering completion callbacks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallbackContext:
    label: str


@workflow.defn
class FailingHandlerWorkflow:
    @workflow.run
    async def run(self, input: EchoInput) -> EchoOutput:
        raise ApplicationError(input.value, "detail", non_retryable=True)


@nexusrpc.service
class CallbackTestService:
    fail_async: nexusrpc.Operation[EchoInput, EchoOutput]


@service_handler(service=CallbackTestService)
class CallbackTestServiceHandler:
    @workflow_run_operation
    async def fail_async(
        self, ctx: WorkflowRunOperationContext, input: EchoInput
    ) -> nexus.WorkflowHandle[EchoOutput]:
        return await ctx.start_workflow(
            FailingHandlerWorkflow.run,
            input,
            id=str(uuid.uuid4()),
        )


@nexusrpc.handler.service_handler(name="callback.test.NotificationService")
class EchoOnCompleteHandler:
    def __init__(self) -> None:
        # Keyed by label so redelivered callbacks don't count twice
        self.requests: dict[
            str, notifications.OnCompleteRequest[EchoOutput, CallbackContext]
        ] = {}

    @nexusrpc.handler.sync_operation(name="OnComplete")
    async def on_complete(
        self,
        _ctx: nexusrpc.handler.StartOperationContext,
        input: notifications.OnCompleteRequest[EchoOutput, CallbackContext],
    ) -> notifications.OnCompleteResponse:
        self.requests[input.source_context.label] = input
        return notifications.OnCompleteResponse()


@nexusrpc.handler.service_handler(name="callback.test.RawNotificationService")
class RawOnCompleteHandler:
    def __init__(self) -> None:
        self.requests: dict[
            str, notifications.OnCompleteRequest[RawValue, CallbackContext]
        ] = {}

    @nexusrpc.handler.sync_operation(name="OnComplete")
    async def on_complete(
        self,
        _ctx: nexusrpc.handler.StartOperationContext,
        input: notifications.OnCompleteRequest[RawValue, CallbackContext],
    ) -> notifications.OnCompleteResponse:
        self.requests[input.source_context.label] = input
        return notifications.OnCompleteResponse()


@dataclass(frozen=True)
class RecordCompletionInput:
    label: str
    output: EchoOutput


@nexusrpc.handler.service_handler(name="callback.test.ActivityNotificationService")
class ActivityOnCompleteHandler:
    def __init__(self) -> None:
        self.outputs: dict[str, EchoOutput] = {}

    @activity.defn
    async def record_completion(
        self, input: RecordCompletionInput
    ) -> notifications.OnCompleteResponse:
        self.outputs[input.label] = input.output
        return notifications.OnCompleteResponse()

    @nexus.temporal_operation
    async def on_complete(
        self,
        _ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: notifications.OnCompleteRequest[EchoOutput, CallbackContext],
    ) -> nexus.TemporalOperationResult[notifications.OnCompleteResponse]:
        assert isinstance(input.result, OnCompleteRequestResultSuccess)
        return await client.start_activity(
            self.record_completion,
            RecordCompletionInput(
                label=input.source_context.label, output=input.result.value
            ),
            id=f"on-complete-{input.source_context.label}",
            start_to_close_timeout=timedelta(seconds=10),
        )


@asynccontextmanager
async def _callback_workers(
    client: Client,
    env: WorkflowEnvironment,
    completion_handler: object,
    operation_handler: StandaloneTestServiceHandler | None = None,
    completion_activities: Sequence[Callable[..., Any]] = (),
) -> AsyncIterator[tuple[str, str]]:
    """Run operation and completion workers, yielding (endpoint, completion queue)."""
    operation_task_queue = str(uuid.uuid4())
    completion_task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(operation_task_queue)
    async with (
        Worker(
            client,
            task_queue=operation_task_queue,
            nexus_service_handlers=[
                operation_handler or StandaloneTestServiceHandler(),
                CallbackTestServiceHandler(),
            ],
            workflows=[
                EchoHandlerWorkflow,
                BlockingHandlerWorkflow,
                FailingHandlerWorkflow,
            ],
        ),
        Worker(
            client,
            task_queue=completion_task_queue,
            nexus_service_handlers=[completion_handler],
            activities=completion_activities,
        ),
    ):
        endpoint = await env.create_nexus_endpoint(endpoint_name, operation_task_queue)
        try:
            yield endpoint_name, completion_task_queue
        finally:
            await env.delete_nexus_endpoint(endpoint)


@pytest.mark.requires_local_server
async def test_completion_callback_delivers_success(
    client: Client, env: WorkflowEnvironment
):
    """A successful operation delivers its result and source context to the handler."""
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )
    completion_handler = EchoOnCompleteHandler()
    async with _callback_workers(client, env, completion_handler) as (
        endpoint_name,
        completion_task_queue,
    ):
        handle = await client.create_nexus_client(
            service=StandaloneTestService, endpoint=endpoint_name
        ).start_operation(
            StandaloneTestService.echo_async,
            EchoInput(value="hello"),
            id=str(uuid.uuid4()),
            schedule_to_close_timeout=timedelta(seconds=30),
            completion_callbacks=[
                nexus.create_completion_callback(
                    operation=EchoOnCompleteHandler.on_complete,
                    task_queue=completion_task_queue,
                    source_context=CallbackContext(label="success"),
                )
            ],
        )

        assert await handle.result() == EchoOutput(value="hello")

        async def received_callbacks() -> None:
            assert set(completion_handler.requests) == {"success"}

        await assert_eventually(received_callbacks)
        request = completion_handler.requests["success"]
        assert request.source_context == CallbackContext(label="success")
        assert request.result == OnCompleteRequestResultSuccess(EchoOutput("hello"))


@pytest.mark.requires_local_server
async def test_completion_callback_delivers_failure(
    client: Client, env: WorkflowEnvironment
):
    """A failed operation delivers the handler workflow's failure to the handler."""
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )
    completion_handler = EchoOnCompleteHandler()
    async with _callback_workers(client, env, completion_handler) as (
        endpoint_name,
        completion_task_queue,
    ):
        handle = await client.create_nexus_client(
            service=CallbackTestService, endpoint=endpoint_name
        ).start_operation(
            CallbackTestService.fail_async,
            EchoInput(value="callback test failure"),
            id=str(uuid.uuid4()),
            schedule_to_close_timeout=timedelta(seconds=30),
            completion_callbacks=[
                nexus.create_completion_callback(
                    operation=EchoOnCompleteHandler.on_complete,
                    task_queue=completion_task_queue,
                    source_context=CallbackContext(label="failure"),
                )
            ],
        )

        with pytest.raises(NexusOperationFailureError):
            await handle.result()

        async def received_callbacks() -> None:
            assert set(completion_handler.requests) == {"failure"}

        await assert_eventually(received_callbacks)
        request = completion_handler.requests["failure"]
        assert request.source_context == CallbackContext(label="failure")
        assert isinstance(request.result, OnCompleteRequestResultFailure)
        error = request.result.value
        assert isinstance(error, ApplicationError)
        assert error.message == "callback test failure"
        assert error.non_retryable


@pytest.mark.requires_local_server
@pytest.mark.parametrize("outcome", ["cancel", "terminate"])
async def test_completion_callback_delivers_cancellation_and_termination(
    client: Client, env: WorkflowEnvironment, outcome: str
):
    """Canceled and terminated operations deliver their failure to the handler."""
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )
    operation_handler = StandaloneTestServiceHandler()
    completion_handler = EchoOnCompleteHandler()
    blocking_input = f"{outcome}-{uuid.uuid4()}"
    async with _callback_workers(
        client, env, completion_handler, operation_handler
    ) as (endpoint_name, completion_task_queue):
        handle = await client.create_nexus_client(
            service=StandaloneTestService, endpoint=endpoint_name
        ).start_operation(
            StandaloneTestService.blocking_async,
            EchoInput(value=blocking_input),
            id=str(uuid.uuid4()),
            schedule_to_close_timeout=timedelta(seconds=30),
            completion_callbacks=[
                nexus.create_completion_callback(
                    operation=EchoOnCompleteHandler.on_complete,
                    task_queue=completion_task_queue,
                    source_context=CallbackContext(label=outcome),
                )
            ],
        )
        await asyncio.wait_for(operation_handler.started_blocking.wait(), timeout=10)

        if outcome == "cancel":
            await handle.cancel(reason="callback test cancel")
        else:
            await handle.terminate(reason="callback test termination")
            # Terminating the operation leaves its handler workflow running
            await client.get_workflow_handle(
                f"blocking_async-{blocking_input}"
            ).terminate()

        with pytest.raises(NexusOperationFailureError):
            await handle.result()

        async def received_callbacks() -> None:
            assert set(completion_handler.requests) == {outcome}

        await assert_eventually(received_callbacks)
        request = completion_handler.requests[outcome]
        assert request.source_context == CallbackContext(label=outcome)
        assert isinstance(request.result, OnCompleteRequestResultFailure)
        if outcome == "cancel":
            assert isinstance(request.result.value, CancelledError)
        else:
            assert isinstance(request.result.value, TerminatedError)
            assert request.result.value.message == "callback test termination"


@pytest.mark.requires_local_server
async def test_multiple_completion_callbacks_on_operation(
    client: Client, env: WorkflowEnvironment
):
    """Every callback attached to an operation is passed to interceptors and delivered."""
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )
    interceptor = _RecordingInterceptor()
    config = client.config()
    config["interceptors"] = [interceptor]
    intercepted_client = Client(**config)
    completion_handler = EchoOnCompleteHandler()
    async with _callback_workers(client, env, completion_handler) as (
        endpoint_name,
        completion_task_queue,
    ):
        completion_callbacks = [
            nexus.create_completion_callback(
                operation=EchoOnCompleteHandler.on_complete,
                task_queue=completion_task_queue,
                source_context=CallbackContext(label=label),
            )
            for label in ("first", "second")
        ]
        handle = await intercepted_client.create_nexus_client(
            service=StandaloneTestService, endpoint=endpoint_name
        ).start_operation(
            StandaloneTestService.echo_async,
            EchoInput(value="hello"),
            id=str(uuid.uuid4()),
            schedule_to_close_timeout=timedelta(seconds=30),
            completion_callbacks=completion_callbacks,
        )

        [start_input] = interceptor.start_calls
        assert list(start_input.completion_callbacks) == completion_callbacks
        assert await handle.result() == EchoOutput(value="hello")

        async def received_callbacks() -> None:
            assert set(completion_handler.requests) == {"first", "second"}

        await assert_eventually(received_callbacks)
        for request in completion_handler.requests.values():
            assert request.result == OnCompleteRequestResultSuccess(EchoOutput("hello"))


@pytest.mark.requires_local_server
async def test_raw_value_completion_handler_for_multiple_operations(
    client: Client, env: WorkflowEnvironment
):
    """A RawValue handler receives completions from operations with different results."""
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )
    operation_handler = StandaloneTestServiceHandler()
    completion_handler = RawOnCompleteHandler()
    blocking_input = f"multiple-operations-{uuid.uuid4()}"
    async with _callback_workers(
        client, env, completion_handler, operation_handler
    ) as (endpoint_name, completion_task_queue):
        nexus_client = client.create_nexus_client(
            service=StandaloneTestService, endpoint=endpoint_name
        )
        echo_handle = await nexus_client.start_operation(
            StandaloneTestService.echo_async,
            EchoInput(value="echo"),
            id=str(uuid.uuid4()),
            schedule_to_close_timeout=timedelta(seconds=30),
            completion_callbacks=[
                nexus.create_completion_callback(
                    operation=RawOnCompleteHandler.on_complete,
                    task_queue=completion_task_queue,
                    source_context=CallbackContext(label="echo_async"),
                )
            ],
        )
        blocking_handle = await nexus_client.start_operation(
            StandaloneTestService.blocking_async,
            EchoInput(value=blocking_input),
            id=str(uuid.uuid4()),
            schedule_to_close_timeout=timedelta(seconds=30),
            completion_callbacks=[
                nexus.create_completion_callback(
                    operation=RawOnCompleteHandler.on_complete,
                    task_queue=completion_task_queue,
                    source_context=CallbackContext(label="blocking_async"),
                )
            ],
        )

        await asyncio.wait_for(operation_handler.started_blocking.wait(), timeout=10)
        await client.get_workflow_handle(
            f"blocking_async-{blocking_input}"
        ).start_update(
            BlockingHandlerWorkflow.unblock,
            wait_for_stage=WorkflowUpdateStage.COMPLETED,
        )

        assert await echo_handle.result() == EchoOutput(value="echo")
        assert await blocking_handle.result() == EchoOutput(value=blocking_input)
        expected_results = {
            "echo_async": EchoOutput(value="echo"),
            "blocking_async": EchoOutput(value=blocking_input),
        }

        async def received_callbacks() -> None:
            assert set(completion_handler.requests) == set(expected_results)

        await assert_eventually(received_callbacks)
        for label, request in completion_handler.requests.items():
            assert isinstance(request.result, OnCompleteRequestResultSuccess)
            # A RawValue handler receives the payload, so convert it here
            result = client.data_converter.payload_converter.from_payload(
                request.result.value.payload, EchoOutput
            )
            assert result == expected_results[label]


@pytest.mark.requires_local_server
async def test_temporal_operation_completion_handler_starts_activity(
    client: Client, env: WorkflowEnvironment
):
    """A temporal_operation completion handler can start a standalone activity."""
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )
    completion_handler = ActivityOnCompleteHandler()
    label = str(uuid.uuid4())
    async with _callback_workers(
        client,
        env,
        completion_handler,
        completion_activities=[completion_handler.record_completion],
    ) as (endpoint_name, completion_task_queue):
        result = await client.create_nexus_client(
            service=StandaloneTestService, endpoint=endpoint_name
        ).execute_operation(
            StandaloneTestService.echo_async,
            EchoInput(value="hello"),
            id=str(uuid.uuid4()),
            schedule_to_close_timeout=timedelta(seconds=30),
            completion_callbacks=[
                nexus.create_completion_callback(
                    operation=ActivityOnCompleteHandler.on_complete,
                    task_queue=completion_task_queue,
                    source_context=CallbackContext(label=label),
                )
            ],
        )

        assert result == EchoOutput(value="hello")

        async def activity_recorded_output() -> None:
            assert completion_handler.outputs == {label: EchoOutput(value="hello")}

        await assert_eventually(activity_recorded_output)
        activity_handle = client.get_activity_handle(
            f"on-complete-{label}", result_type=notifications.OnCompleteResponse
        )
        assert await activity_handle.result() == notifications.OnCompleteResponse()


@pytest.mark.requires_local_server
@pytest.mark.parametrize("success", [True, False])
async def test_completion_callback_by_name_with_codec(
    client: Client, env: WorkflowEnvironment, success: bool
):
    """Name-based callbacks from execute_operation round-trip through a payload codec."""
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )
    config = client.config()
    config["data_converter"] = dataclasses.replace(
        client.data_converter, payload_codec=PrefixCodec()
    )
    codec_client = Client(**config)
    completion_handler = RawOnCompleteHandler()
    async with _callback_workers(codec_client, env, completion_handler) as (
        endpoint_name,
        completion_task_queue,
    ):
        completion_callbacks = [
            nexus.create_completion_callback(
                service="callback.test.RawNotificationService",
                operation="OnComplete",
                task_queue=completion_task_queue,
                source_context=CallbackContext(label="codec"),
            )
        ]
        if success:
            result = await codec_client.create_nexus_client(
                service=StandaloneTestService, endpoint=endpoint_name
            ).execute_operation(
                StandaloneTestService.echo_async,
                EchoInput(value="encoded"),
                id=str(uuid.uuid4()),
                schedule_to_close_timeout=timedelta(seconds=30),
                completion_callbacks=completion_callbacks,
            )
            assert result == EchoOutput(value="encoded")
        else:
            with pytest.raises(NexusOperationFailureError):
                await codec_client.create_nexus_client(
                    service=CallbackTestService, endpoint=endpoint_name
                ).execute_operation(
                    CallbackTestService.fail_async,
                    EchoInput(value="encoded failure"),
                    id=str(uuid.uuid4()),
                    schedule_to_close_timeout=timedelta(seconds=30),
                    completion_callbacks=completion_callbacks,
                )

        async def received_callbacks() -> None:
            assert set(completion_handler.requests) == {"codec"}

        await assert_eventually(received_callbacks)
        request = completion_handler.requests["codec"]
        assert request.source_context == CallbackContext(label="codec")
        if success:
            assert isinstance(request.result, OnCompleteRequestResultSuccess)
            # The worker decoded the codec before handing the handler the raw value
            raw = request.result.value.payload
            assert raw.metadata["encoding"] != b"test/prefix"
            assert codec_client.data_converter.payload_converter.from_payload(
                raw, EchoOutput
            ) == EchoOutput(value="encoded")
        else:
            assert isinstance(request.result, OnCompleteRequestResultFailure)
            error = request.result.value
            assert isinstance(error, ApplicationError)
            assert error.message == "encoded failure"
            # Failure details can only be read if the worker decoded the codec
            assert error.details == ("detail",)


@pytest.mark.requires_local_server
async def test_completion_callback_typed_handler_with_codec(
    client: Client, env: WorkflowEnvironment
):
    """A typed handler receives the result decoded through a payload codec."""
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )
    config = client.config()
    config["data_converter"] = dataclasses.replace(
        client.data_converter, payload_codec=PrefixCodec()
    )
    codec_client = Client(**config)
    completion_handler = EchoOnCompleteHandler()
    async with _callback_workers(codec_client, env, completion_handler) as (
        endpoint_name,
        completion_task_queue,
    ):
        result = await codec_client.create_nexus_client(
            service=StandaloneTestService, endpoint=endpoint_name
        ).execute_operation(
            StandaloneTestService.echo_async,
            EchoInput(value="encoded"),
            id=str(uuid.uuid4()),
            schedule_to_close_timeout=timedelta(seconds=30),
            completion_callbacks=[
                nexus.create_completion_callback(
                    operation=EchoOnCompleteHandler.on_complete,
                    task_queue=completion_task_queue,
                    source_context=CallbackContext(label="typed-codec"),
                )
            ],
        )

        assert result == EchoOutput(value="encoded")

        async def received_callbacks() -> None:
            assert set(completion_handler.requests) == {"typed-codec"}

        await assert_eventually(received_callbacks)
        request = completion_handler.requests["typed-codec"]
        assert request.source_context == CallbackContext(label="typed-codec")
        assert request.result == OnCompleteRequestResultSuccess(EchoOutput("encoded"))
