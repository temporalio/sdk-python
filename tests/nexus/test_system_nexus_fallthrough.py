from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.client import Client
from temporalio.nexus.system import TEMPORAL_SYSTEM_ENDPOINT
from temporalio.worker import (
    Interceptor,
    StartNexusOperationInput,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)
from tests.helpers import new_worker

PLUGIN_SERVICE = "test.plugin.v1.EchoService"


class _DoneHandle(workflow.NexusOperationHandle[Any]):
    def __init__(self, value: Any) -> None:
        self._value = value

    def cancel(self) -> bool:
        return False

    def __await__(self) -> Generator[Any, Any, Any]:
        return self._result().__await__()

    async def _result(self) -> Any:
        return self._value

    @property
    def operation_token(self) -> str | None:
        return None


class _EchoOutboundInterceptor(WorkflowOutboundInterceptor):
    async def start_system_nexus_operation(
        self, input: StartNexusOperationInput[Any, Any]
    ) -> workflow.NexusOperationHandle[Any]:
        if input.service == PLUGIN_SERVICE:
            return _DoneHandle(f"{input.operation_name}: {input.input}")
        return await super().start_system_nexus_operation(input)


class _EchoInboundInterceptor(WorkflowInboundInterceptor):
    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        super().init(_EchoOutboundInterceptor(outbound))


class EchoServiceInterceptor(Interceptor):
    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor] | None:
        return _EchoInboundInterceptor


@workflow.defn
class PluginServiceWorkflow:
    @workflow.run
    async def run(self) -> str:
        client = workflow.create_nexus_client(
            service=PLUGIN_SERVICE, endpoint=TEMPORAL_SYSTEM_ENDPOINT
        )
        handle: workflow.NexusOperationHandle[Any] = await client.start_operation(
            "Echo", "hello"
        )
        return await handle


async def test_unknown_system_nexus_operation_reaches_generic_hook(client: Client):
    async with new_worker(
        client, PluginServiceWorkflow, interceptors=[EchoServiceInterceptor()]
    ) as worker:
        result = await client.execute_workflow(
            PluginServiceWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=10),
        )
    assert result == "Echo: hello"
