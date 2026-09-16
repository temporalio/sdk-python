from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import timedelta
from typing import Any, Generic, TypeVar, cast
from uuid import uuid4

import nexusrpc
import nexusrpc.handler
import pytest

import temporalio.activity
import temporalio.api.common.v1
import temporalio.client
import temporalio.common
import temporalio.converter
import temporalio.testing
import temporalio.worker
import temporalio.workflow


class DeclaredValue(str):
    pass


class RuntimeValue(str):
    pass


class DeclaredConverter(temporalio.converter.TransferTypeConverter[DeclaredValue, str]):
    transfer_type = str

    def to_transfer_type(self, value: DeclaredValue) -> str:
        return f"declared:{value}"

    def from_transfer_type(
        self, value: str, type_hint: type[DeclaredValue]
    ) -> DeclaredValue:
        assert value.startswith("declared:")
        return DeclaredValue(value.removeprefix("declared:"))


class RuntimeConverter(temporalio.converter.TransferTypeConverter[RuntimeValue, str]):
    transfer_type = str

    def to_transfer_type(self, value: RuntimeValue) -> str:
        return f"runtime:{value}"

    def from_transfer_type(
        self, value: str, type_hint: type[RuntimeValue]
    ) -> RuntimeValue:
        return RuntimeValue(value.removeprefix("runtime:"))


temporalio.converter.transfer_type_convertible(DeclaredConverter)(DeclaredValue)
temporalio.converter.transfer_type_convertible(RuntimeConverter)(RuntimeValue)


@pytest.mark.parametrize(
    ("hints", "expected"),
    [
        ([DeclaredValue], "declared:value"),
        ([RuntimeValue], "runtime:value"),
        ([str], "value"),
        ([None], "value"),
        (None, "value"),
        ([], "value"),
        ([DeclaredValue, int], "declared:value"),
    ],
)
async def test_transfer_serialization_type_selection(
    hints: list[type | None] | None, expected: str
):
    converter = temporalio.converter.DataConverter.default
    payloads = await converter.encode_with_type_hints([RuntimeValue("value")], hints)
    assert await converter.decode(payloads) == [expected]


T = TypeVar("T")


class GenericValue(Generic[T]):
    pass


class GenericConverter(
    temporalio.converter.TransferTypeConverter[GenericValue[Any], str]
):
    transfer_type = str

    def to_transfer_type(self, value: GenericValue[Any]) -> str:
        return "generic converter"

    def from_transfer_type(
        self, value: str, type_hint: type[GenericValue[Any]]
    ) -> GenericValue[Any]:
        return GenericValue()


temporalio.converter.transfer_type_convertible(GenericConverter)(GenericValue)


async def test_transfer_serialization_generic_hint():
    converter = temporalio.converter.DataConverter.default
    payloads = await converter.encode_with_type_hints([object()], [GenericValue[int]])
    assert await converter.decode(payloads) == ["generic converter"]


async def test_transfer_serialization_legacy_overrides_and_nested_conversion():
    calls: list[str] = []
    typed_values = (RuntimeValue("typed"),)
    untyped_values = [RuntimeValue("untyped")]

    class LegacyPayloadConverter(temporalio.converter.DefaultPayloadConverter):
        def to_payloads(
            self, values: Sequence[Any]
        ) -> list[temporalio.api.common.v1.Payload]:
            calls.append("payload")
            nested = (
                temporalio.converter.DataConverter.default.payload_converter.to_payload(
                    RuntimeValue("nested")
                )
            )
            assert (
                temporalio.converter.DataConverter.default.payload_converter.from_payload(
                    nested
                )
                == "nested"
            )
            return super().to_payloads(values)

    class LegacyDataConverter(temporalio.converter.DataConverter):
        async def encode(
            self, values: Sequence[Any]
        ) -> list[temporalio.api.common.v1.Payload]:
            calls.append("data")
            assert values is typed_values or values is untyped_values
            nested = await temporalio.converter.DataConverter.default.encode(
                [RuntimeValue("before")]
            )
            assert await temporalio.converter.DataConverter.default.decode(nested) == [
                "before"
            ]
            await asyncio.sleep(0)
            return await super().encode(values)

    converter = LegacyDataConverter(payload_converter_class=LegacyPayloadConverter)
    typed, untyped = await asyncio.gather(
        converter.encode_with_type_hints(typed_values, [DeclaredValue]),
        converter.encode(untyped_values),
    )
    assert await converter.decode(typed) == ["declared:typed"]
    assert await converter.decode(untyped) == ["untyped"]
    assert calls.count("data") == calls.count("payload") == 2


def test_transfer_serialization_payload_override_preserves_sequence():
    expected_values = (RuntimeValue("value"),)
    inner = temporalio.converter.DataConverter.default.payload_converter

    class LegacyPayloadConverter(temporalio.converter.DefaultPayloadConverter):
        def to_payloads(
            self, values: Sequence[Any]
        ) -> list[temporalio.api.common.v1.Payload]:
            assert values is expected_values
            return inner.to_payloads(values)

    payloads = LegacyPayloadConverter().to_payloads_with_type_hints(
        expected_values, [DeclaredValue]
    )
    assert inner.from_payloads(payloads) == ["declared:value"]
    assert inner.from_payloads(inner.to_payloads(expected_values)) == ["value"]


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_transfer_serialization_context_reset_on_error(
    error: type[BaseException],
):
    expected_values = [RuntimeValue("value")]

    class FailingDataConverter(temporalio.converter.DataConverter):
        async def encode(
            self, values: Sequence[Any]
        ) -> list[temporalio.api.common.v1.Payload]:
            assert values is expected_values
            raise error()

    with pytest.raises(error):
        await FailingDataConverter().encode_with_type_hints(
            expected_values, [DeclaredValue]
        )
    converter = temporalio.converter.DataConverter.default
    assert await converter.decode(await converter.encode(expected_values)) == ["value"]


async def test_transfer_serialization_restores_outer_hints():
    expected_values = [RuntimeValue("value")]
    inner = temporalio.converter.DataConverter.default

    class NestedDataConverter(temporalio.converter.DataConverter):
        async def encode(
            self, values: Sequence[Any]
        ) -> list[temporalio.api.common.v1.Payload]:
            payloads = await inner.encode_with_type_hints(values, [str])
            assert await inner.decode(payloads) == ["value"]
            return await super().encode(values)

    payloads = await NestedDataConverter().encode_with_type_hints(
        expected_values, [DeclaredValue]
    )
    assert await inner.decode(payloads) == ["declared:value"]


async def test_transfer_serialization_clears_hints_before_conversion(
    monkeypatch: pytest.MonkeyPatch,
):
    values = [RuntimeValue("value")]
    converter = temporalio.converter.DataConverter.default
    original = DeclaredConverter.to_transfer_type

    def convert(self: DeclaredConverter, value: DeclaredValue) -> str:
        inner = converter.payload_converter
        assert inner.from_payloads(inner.to_payloads(values)) == ["value"]
        return original(self, value)

    monkeypatch.setattr(DeclaredConverter, "to_transfer_type", convert)
    payloads = await converter.encode_with_type_hints(values, [DeclaredValue])
    assert await converter.decode(payloads) == ["declared:value"]


async def test_transfer_serialization_expires_inherited_hints():
    expected_values = [RuntimeValue("value")]
    ready = asyncio.Event()
    converter = temporalio.converter.DataConverter.default
    tasks: list[asyncio.Task[list[temporalio.api.common.v1.Payload]]] = []

    async def later() -> list[temporalio.api.common.v1.Payload]:
        await ready.wait()
        return await converter.encode(expected_values)

    class SpawningDataConverter(temporalio.converter.DataConverter):
        async def encode(
            self, values: Sequence[Any]
        ) -> list[temporalio.api.common.v1.Payload]:
            tasks.append(asyncio.create_task(later()))
            return await super().encode(values)

    try:
        payloads = await SpawningDataConverter().encode_with_type_hints(
            expected_values, [DeclaredValue]
        )
        assert await converter.decode(payloads) == ["declared:value"]
    finally:
        ready.set()
    [payloads] = await asyncio.gather(*tasks)
    assert await converter.decode(payloads) == ["value"]


async def test_transfer_serialization_raw_value():
    converter = temporalio.converter.DataConverter.default
    [raw] = await converter.encode(["already encoded"])
    assert await converter.encode_with_type_hints(
        [temporalio.common.RawValue(raw)], [DeclaredValue]
    ) == [raw]


@temporalio.activity.defn
async def typed_activity(value: DeclaredValue) -> DeclaredValue:
    assert type(value) is DeclaredValue
    return cast(DeclaredValue, str(value))


@temporalio.workflow.defn
class TypedChildWorkflow:
    def __init__(self) -> None:
        self.signal_count = 0

    @temporalio.workflow.run
    async def run(self, value: DeclaredValue) -> DeclaredValue:
        assert type(value) is DeclaredValue
        await temporalio.workflow.wait_condition(lambda: self.signal_count == 2)
        return cast(DeclaredValue, str(value))

    @temporalio.workflow.signal
    def signal(self, value: DeclaredValue) -> None:
        assert type(value) is DeclaredValue
        self.signal_count += 1


@temporalio.workflow.defn
class TypedWorkflow:
    def __init__(self) -> None:
        self.signalled = False

    @temporalio.workflow.run
    async def run(self, value: DeclaredValue, continued: bool = False) -> DeclaredValue:
        assert type(value) is DeclaredValue
        if not continued:
            temporalio.workflow.continue_as_new(args=[str(value), True])
        await temporalio.workflow.wait_condition(lambda: self.signalled)
        value = await temporalio.workflow.execute_activity(
            typed_activity,
            cast(DeclaredValue, str(value)),
            start_to_close_timeout=timedelta(seconds=10),
        )
        value = await temporalio.workflow.execute_local_activity(
            typed_activity,
            cast(DeclaredValue, str(value)),
            start_to_close_timeout=timedelta(seconds=10),
        )
        child = await temporalio.workflow.start_child_workflow(
            TypedChildWorkflow.run, cast(DeclaredValue, str(value))
        )
        await child.signal(TypedChildWorkflow.signal, cast(DeclaredValue, str(value)))
        external: temporalio.workflow.ExternalWorkflowHandle[TypedChildWorkflow] = (
            temporalio.workflow.get_external_workflow_handle_for(
                TypedChildWorkflow.run, child.id
            )
        )
        await external.signal(
            TypedChildWorkflow.signal, cast(DeclaredValue, str(value))
        )
        value = await child
        return cast(DeclaredValue, str(value))

    @temporalio.workflow.signal
    def finish(self, value: DeclaredValue) -> None:
        assert type(value) is DeclaredValue
        self.signalled = True

    @temporalio.workflow.query
    def echo_query(self, value: DeclaredValue) -> DeclaredValue:
        assert type(value) is DeclaredValue
        return cast(DeclaredValue, str(value))

    @temporalio.workflow.update
    async def echo_update(self, value: DeclaredValue) -> DeclaredValue:
        assert type(value) is DeclaredValue
        return cast(DeclaredValue, str(value))

    @temporalio.workflow.query
    def continued(self) -> bool:
        return bool(temporalio.workflow.info().continued_run_id)


async def test_transfer_serialization_workflow_and_activity(
    client: temporalio.client.Client,
):
    task_queue = str(uuid4())
    value = cast(DeclaredValue, str("value"))
    async with temporalio.worker.Worker(
        client,
        task_queue=task_queue,
        workflows=[TypedWorkflow, TypedChildWorkflow],
        activities=[typed_activity],
    ):
        handle = await client.start_workflow(
            TypedWorkflow.run,
            args=[value, False],
            id=str(uuid4()),
            task_queue=task_queue,
        )
        while not await handle.query(TypedWorkflow.continued):
            await asyncio.sleep(0.01)
        assert await handle.query(TypedWorkflow.echo_query, value) == "value"
        assert await handle.execute_update(TypedWorkflow.echo_update, value) == "value"
        await handle.signal(TypedWorkflow.finish, value)
        assert await handle.result() == "value"
        activity = await client.start_activity(
            typed_activity,
            value,
            id=str(uuid4()),
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=10),
        )
        assert await activity.result() == "value"


async def test_transfer_serialization_schedule(client: temporalio.client.Client):
    action = temporalio.client.ScheduleActionStartWorkflow(
        TypedChildWorkflow.run,
        cast(DeclaredValue, str("scheduled")),
        id=str(uuid4()),
        task_queue="unused",
    )
    proto = await action._to_proto(client)
    assert await client.data_converter.decode(proto.start_workflow.input.payloads) == [
        "declared:scheduled"
    ]


@nexusrpc.service
class TypedService:
    echo: nexusrpc.Operation[DeclaredValue, DeclaredValue]


@nexusrpc.handler.service_handler(service=TypedService)
class TypedServiceHandler:
    @nexusrpc.handler.sync_operation
    async def echo(
        self, _ctx: nexusrpc.handler.StartOperationContext, value: DeclaredValue
    ) -> DeclaredValue:
        assert type(value) is DeclaredValue
        return cast(DeclaredValue, str(value))


@temporalio.workflow.defn
class TypedNexusWorkflow:
    @temporalio.workflow.run
    async def run(self, endpoint: str) -> DeclaredValue:
        client = temporalio.workflow.create_nexus_client(
            service=TypedService, endpoint=endpoint
        )
        operations: Sequence[Any] = [TypedService.echo, TypedServiceHandler.echo]
        for operation in operations:
            result = await client.execute_operation(
                operation,
                cast(DeclaredValue, str("nexus")),
                schedule_to_close_timeout=timedelta(seconds=10),
            )
            assert type(result) is DeclaredValue
        return cast(DeclaredValue, str("nexus"))


@pytest.mark.requires_local_server
async def test_transfer_serialization_nexus(
    env: temporalio.testing.WorkflowEnvironment,
):
    task_queue = str(uuid4())
    endpoint = await env.create_nexus_endpoint(f"typed-{task_queue}", task_queue)
    try:
        async with temporalio.worker.Worker(
            env.client,
            task_queue=task_queue,
            workflows=[TypedNexusWorkflow],
            nexus_service_handlers=[TypedServiceHandler()],
        ):
            nexus_client = env.client.create_nexus_client(
                service=TypedService, endpoint=endpoint.spec.name
            )
            operations: Sequence[Any] = [TypedService.echo, TypedServiceHandler.echo]
            for operation in operations:
                result = await nexus_client.execute_operation(
                    operation,
                    cast(DeclaredValue, str("nexus")),
                    id=str(uuid4()),
                    schedule_to_close_timeout=timedelta(seconds=10),
                )
                assert type(result) is DeclaredValue
                assert result == "nexus"
            assert (
                await env.client.execute_workflow(
                    TypedNexusWorkflow.run,
                    endpoint.spec.name,
                    id=str(uuid4()),
                    task_queue=task_queue,
                )
                == "nexus"
            )
    finally:
        await env.delete_nexus_endpoint(endpoint)


async def test_transfer_serialization_update_with_start(
    client: temporalio.client.Client,
):
    task_queue = str(uuid4())
    value = cast(DeclaredValue, str("value"))
    async with temporalio.worker.Worker(
        client,
        task_queue=task_queue,
        workflows=[TypedWorkflow, TypedChildWorkflow],
        activities=[typed_activity],
    ):
        start = temporalio.client.WithStartWorkflowOperation(
            TypedWorkflow.run,
            args=[value, True],
            id=str(uuid4()),
            task_queue=task_queue,
            id_conflict_policy=temporalio.common.WorkflowIDConflictPolicy.FAIL,
        )
        assert (
            await client.execute_update_with_start_workflow(
                TypedWorkflow.echo_update,
                value,
                start_workflow_operation=start,
            )
            == "value"
        )
        handle = await start.workflow_handle()
        await handle.signal(TypedWorkflow.finish, value)
        assert await handle.result() == "value"
