"""Value handles across a Nexus boundary.

Forwarding a handle *into* an operation is the handler's contract to declare: it
may receive a ``ValueHandle`` and forward it on, or receive the value and have its
own worker resolve the reference. Whether that reference resolves is a property of
the deployment (does the handler's side share the caller's external storage), and
it fails loudly at resolution time when it does not -- so the SDK does not refuse
it.

Deferring an operation's *result* is different: a Nexus resolution is not one of
the command types the worker defers, so a handle there would wrap an
already-decoded payload and decode it a second time on acquisition. That is
refused at the call site.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import nexusrpc
import pytest
from nexusrpc.handler import StartOperationContext, service_handler, sync_operation

import temporalio.nexus
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowHandle
from temporalio.common import ValueHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers import assert_eventually, new_worker
from tests.test_extstore import InMemoryTestDriver
from tests.worker.test_payload_handle import _BIG, _client


@nexusrpc.service
class ForwardService:
    measure: nexusrpc.Operation[ValueHandle[str], int]


@nexusrpc.service
class EchoService:
    echo: nexusrpc.Operation[str, str]


@activity.defn
async def measure_handle(data: ValueHandle[str]) -> int:
    return len(await data.get_value())


@workflow.defn
class HandlerSideWorkflow:
    @workflow.run
    async def run(self, data: ValueHandle[str]) -> int:
        # The handler side only routes: it forwards the caller's reference to its
        # own activity, which is where the bytes are finally read.
        return await workflow.execute_activity(
            measure_handle,
            data,
            start_to_close_timeout=timedelta(seconds=30),
        )


@service_handler(service=ForwardService)
class ForwardServiceHandler:
    @sync_operation
    async def measure(self, ctx: StartOperationContext, data: ValueHandle[str]) -> int:
        # The operation declared a handle, so nothing was downloaded to get here.
        client = temporalio.nexus.client()
        return await client.execute_workflow(
            HandlerSideWorkflow.run,
            data,
            id=f"handler-{uuid.uuid4()}",
            task_queue=_handler_task_queue,
        )


_handler_task_queue = ""


@workflow.defn
class NexusForwardCaller:
    @workflow.run
    async def run(self, endpoint: str, data: ValueHandle[str]) -> int:
        client = workflow.create_nexus_client(service=ForwardService, endpoint=endpoint)
        return await client.execute_operation(ForwardService.measure, data)


@workflow.defn
class NexusResultAsHandleWorkflow:
    @workflow.run
    async def run(self, endpoint: str) -> str:
        client = workflow.create_nexus_client(service=EchoService, endpoint=endpoint)
        # assert-type-error-mypy: 'Argument "output_type" to "execute_operation"'
        return await client.execute_operation(
            EchoService.echo,
            "x",
            output_type=ValueHandle[str],  # type: ignore[arg-type]
        )


@pytest.mark.xfail(
    reason=(
        "A forwarded reference is not retrievable after a Nexus hop on this "
        "branch's base. The claim survives in Payload.data, but the transport "
        "drops the Payload.external_payloads side-field, and _decode_reference "
        "here uses that field's presence as its 'is this a reference' guard. "
        "origin/main has since removed that guard, so re-check this after a "
        "rebase -- and check the remaining half too: Nexus input deserialization "
        "applies the payload codec but never external-storage retrieval."
    ),
    strict=True,
)
async def test_handle_forwards_through_a_nexus_operation(
    env: WorkflowEnvironment,
) -> None:
    global _handler_task_queue
    driver = InMemoryTestDriver()
    client: Client = await _client(env, driver)

    caller_task_queue = f"caller-{uuid.uuid4()}"
    _handler_task_queue = f"handler-{uuid.uuid4()}"
    endpoint = f"endpoint-{uuid.uuid4()}"
    await env.create_nexus_endpoint(endpoint, _handler_task_queue)

    caller = Worker(
        client, task_queue=caller_task_queue, workflows=[NexusForwardCaller]
    )
    handler = Worker(
        client,
        task_queue=_handler_task_queue,
        workflows=[HandlerSideWorkflow],
        activities=[measure_handle],
        nexus_service_handlers=[ForwardServiceHandler()],
    )
    async with caller, handler:
        result = await client.execute_workflow(
            NexusForwardCaller.run,
            args=[endpoint, _BIG],
            id=f"wf-{uuid.uuid4()}",
            task_queue=caller_task_queue,
        )

    assert result == len(_BIG)
    # Uploaded once by the caller; downloaded once, at the activity on the
    # handler side that actually reads it. Neither workflow nor the operation
    # handler ever downloaded it.
    assert driver._store_calls == 1
    assert driver._retrieve_calls == 1


async def test_nexus_result_as_handle_is_rejected(env: WorkflowEnvironment) -> None:
    driver = InMemoryTestDriver()
    client: Client = await _client(env, driver)
    async with new_worker(client, NexusResultAsHandleWorkflow) as worker:
        handle: WorkflowHandle = await client.start_workflow(
            NexusResultAsHandleWorkflow.run,
            "unused-endpoint",
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        async def check() -> None:
            async for event in handle.fetch_history_events():
                if event.HasField("workflow_task_failed_event_attributes"):
                    message = (
                        event.workflow_task_failed_event_attributes.failure.message
                    )
                    assert "TMPRL1111" in message, message
                    return
            raise AssertionError("no workflow task failure yet")

        await assert_eventually(check)
        await handle.terminate()
