import gc
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

import nexusrpc
import nexusrpc.handler
import pytest

import temporalio.api.enums.v1
import temporalio.nexus as nexus
import temporalio.workflow as workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers import assert_event_subsequence
from tests.helpers.nexus import make_nexus_endpoint_name

pytestmark = pytest.mark.requires_local_server


@nexusrpc.service
class EvictionTestService:
    wait: nexusrpc.Operation[str, str]


@workflow.defn
class EvictionTestOperationWorkflow:
    def __init__(self) -> None:
        self.released = False

    @workflow.run
    async def run(self) -> str:
        await workflow.wait_condition(lambda: self.released)
        return "completed"

    @workflow.signal
    def release(self) -> None:
        self.released = True


@nexusrpc.handler.service_handler(service=EvictionTestService)
class EvictionTestServiceHandler:
    @nexus.temporal_operation
    async def wait(
        self,
        _ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        workflow_id: str,
    ) -> nexus.TemporalOperationResult[str]:
        return await client.start_workflow(
            EvictionTestOperationWorkflow.run, id=workflow_id
        )


@dataclass
class EvictionTestCallerInput:
    endpoint: str
    operation_workflow_id: str


@workflow.defn
class EvictionTestCallerWorkflow:
    @workflow.run
    async def run(self, input: EvictionTestCallerInput) -> str:
        client = workflow.create_nexus_client(
            service=EvictionTestService, endpoint=input.endpoint
        )
        return await client.execute_operation(
            EvictionTestService.wait,
            input.operation_workflow_id,
            schedule_to_close_timeout=timedelta(minutes=5),
        )


@pytest.mark.parametrize("operation_state", ["starting", "started"])
@pytest.mark.parametrize("eviction_reason", ["cache-eviction", "shutdown"])
async def test_nexus_caller_eviction(
    env: WorkflowEnvironment,
    operation_state: Literal["starting", "started"],
    eviction_reason: Literal["cache-eviction", "shutdown"],
    caplog: pytest.LogCaptureFixture,
) -> None:
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    caller_id = str(uuid.uuid4())
    caller_queue = str(uuid.uuid4())
    handler_queue = str(uuid.uuid4())
    operation_id = str(uuid.uuid4())
    endpoint = make_nexus_endpoint_name(handler_queue)
    await env.create_nexus_endpoint(endpoint, handler_queue)

    async with AsyncExitStack() as stack:
        handler_worker = Worker(
            env.client,
            task_queue=handler_queue,
            workflows=[EvictionTestOperationWorkflow],
            nexus_service_handlers=[EvictionTestServiceHandler()],
        )
        if operation_state == "started":
            await stack.enter_async_context(handler_worker)

        async with Worker(
            env.client,
            task_queue=caller_queue,
            workflows=[EvictionTestCallerWorkflow],
            max_cached_workflows=1000 if eviction_reason == "shutdown" else 0,
        ):
            handle = await env.client.start_workflow(
                EvictionTestCallerWorkflow.run,
                EvictionTestCallerInput(endpoint, operation_id),
                id=caller_id,
                task_queue=caller_queue,
            )

            expected_events = [
                temporalio.api.enums.v1.EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED
            ]
            if operation_state == "started":
                # The following workflow task must complete so the caller is
                # waiting for the operation's result, not its start response.
                expected_events.extend(
                    [
                        temporalio.api.enums.v1.EventType.EVENT_TYPE_NEXUS_OPERATION_STARTED,
                        temporalio.api.enums.v1.EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED,
                    ]
                )
            await assert_event_subsequence(handle, expected_events)

        gc.collect()
        assert "Task was destroyed but it is pending!" not in caplog.text

        if operation_state == "starting":
            # Keep start pending until the original caller worker stops.
            await stack.enter_async_context(handler_worker)

        # A different worker must still receive the original operation's result.
        async with Worker(
            env.client,
            task_queue=caller_queue,
            workflows=[EvictionTestCallerWorkflow],
        ):
            await assert_event_subsequence(
                handle,
                [temporalio.api.enums.v1.EventType.EVENT_TYPE_NEXUS_OPERATION_STARTED],
            )
            operation_handle = env.client.get_workflow_handle(
                operation_id, result_type=str
            )
            await operation_handle.signal(EvictionTestOperationWorkflow.release)
            assert await handle.result() == "completed"

        history = await handle.fetch_history()
        assert not any(
            event.event_type
            == temporalio.api.enums.v1.EventType.EVENT_TYPE_NEXUS_OPERATION_CANCEL_REQUESTED
            for event in history.events
        )
        gc.collect()
        assert "Task was destroyed but it is pending!" not in caplog.text
