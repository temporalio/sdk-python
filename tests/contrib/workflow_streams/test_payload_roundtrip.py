"""Regression guards for the workflow_streams Payload wire format.

1. The default JSON converter does not handle ``Payload`` embedded in a
   dataclass — serialization fails with ``TypeError``. This rules out a
   naive nested-Payload wire format.
2. A proto-serialized ``Payload`` inside a dataclass does round-trip.
   This is the wire format used: base64 of ``Payload.SerializeToString()``
   inside ``PublishEntry``/``_WorkflowStreamWireItem``, surfacing
   ``Payload`` (or a decoded value via ``result_type=``) at the user API.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass, field

import pytest

from temporalio import workflow
from temporalio.api.common.v1 import Payload
from temporalio.client import Client
from tests.helpers import new_worker


@dataclass
class NestedPayloadEnvelope:
    items: list[Payload] = field(default_factory=list)


@dataclass
class SerializedEntry:
    topic: str
    data: str  # base64(Payload.SerializeToString())


@dataclass
class SerializedEnvelope:
    items: list[SerializedEntry] = field(default_factory=list)


@workflow.defn
class NestedPayloadWorkflow:
    def __init__(self) -> None:
        self._received: NestedPayloadEnvelope | None = None

    @workflow.signal
    def receive(self, envelope: NestedPayloadEnvelope) -> None:
        self._received = envelope

    @workflow.query
    def decoded_strings(self) -> list[str]:
        assert self._received is not None
        conv = workflow.payload_converter()
        return [conv.from_payload(p, str) for p in self._received.items]

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self._received is not None)


@workflow.defn
class SerializedPayloadWorkflow:
    def __init__(self) -> None:
        self._received: SerializedEnvelope | None = None

    @workflow.signal
    def receive(self, envelope: SerializedEnvelope) -> None:
        self._received = envelope

    @workflow.query
    def decoded_strings(self) -> list[str]:
        assert self._received is not None
        conv = workflow.payload_converter()
        out: list[str] = []
        for entry in self._received.items:
            p = Payload()
            p.ParseFromString(base64.b64decode(entry.data))
            out.append(conv.from_payload(p, str))
        return out

    @workflow.query
    def topics(self) -> list[str]:
        assert self._received is not None
        return [e.topic for e in self._received.items]

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self._received is not None)


@pytest.mark.asyncio
async def test_nested_payload_in_dataclass_fails(client: Client) -> None:
    """Confirm the load-bearing negative result: Payload inside dataclass doesn't serialize."""
    conv = client.data_converter.payload_converter
    payloads = [conv.to_payloads([v])[0] for v in ["hello", "world"]]
    envelope = NestedPayloadEnvelope(items=payloads)

    async with new_worker(client, NestedPayloadWorkflow) as worker:
        handle = await client.start_workflow(
            NestedPayloadWorkflow.run,
            id=f"nested-payload-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        with pytest.raises(TypeError, match="Payload is not JSON serializable"):
            await handle.signal(NestedPayloadWorkflow.receive, envelope)
        await handle.terminate()


@pytest.mark.asyncio
async def test_serialized_payload_fallback_round_trips(client: Client) -> None:
    """Proto-serialize Payload -> base64 -> dataclass round-trips through signal."""
    conv = client.data_converter.payload_converter
    originals = ["hello", "world", "payload"]
    payloads = [conv.to_payloads([v])[0] for v in originals]
    envelope = SerializedEnvelope(
        items=[
            SerializedEntry(
                topic=f"t{i}",
                data=base64.b64encode(p.SerializeToString()).decode("ascii"),
            )
            for i, p in enumerate(payloads)
        ]
    )

    async with new_worker(client, SerializedPayloadWorkflow) as worker:
        handle = await client.start_workflow(
            SerializedPayloadWorkflow.run,
            id=f"serialized-payload-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.signal(SerializedPayloadWorkflow.receive, envelope)
        decoded = await handle.query(SerializedPayloadWorkflow.decoded_strings)
        assert decoded == originals
        topics = await handle.query(SerializedPayloadWorkflow.topics)
        assert topics == ["t0", "t1", "t2"]
        await handle.result()
