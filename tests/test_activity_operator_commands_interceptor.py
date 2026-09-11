from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

import temporalio.api.activity.v1
import temporalio.api.workflowservice.v1
from temporalio.client import (
    ActivityOptionsKeys,
    Client,
    Interceptor,
    OutboundInterceptor,
    PauseActivityInput,
    UnpauseActivityInput,
    UpdateActivityOptionsInput,
)
from temporalio.service import ServiceClient


class TracingClientInterceptor(Interceptor):
    def __init__(self) -> None:
        super().__init__()
        self.traces: list[tuple[str, Any]] = []

    def intercept_client(self, next: OutboundInterceptor) -> OutboundInterceptor:
        return TracingClientOutboundInterceptor(self, next)


class TracingClientOutboundInterceptor(OutboundInterceptor):
    def __init__(self, parent: TracingClientInterceptor, next: OutboundInterceptor):
        super().__init__(next)
        self._parent = parent

    async def pause_activity(self, input: PauseActivityInput) -> None:
        self._parent.traces.append(("pause_activity", input))
        return await super().pause_activity(input)

    async def unpause_activity(self, input: UnpauseActivityInput) -> None:
        self._parent.traces.append(("unpause_activity", input))
        return await super().unpause_activity(input)

    async def update_activity_options(self, input: UpdateActivityOptionsInput) -> Any:
        self._parent.traces.append(("update_activity_options", input))
        return await super().update_activity_options(input)


def _stub_service() -> Any:
    service = Mock()
    service.pause_activity_execution = AsyncMock(
        return_value=temporalio.api.workflowservice.v1.PauseActivityExecutionResponse()
    )
    service.unpause_activity_execution = AsyncMock(
        return_value=temporalio.api.workflowservice.v1.UnpauseActivityExecutionResponse()
    )
    service.update_activity_execution_options = AsyncMock(
        return_value=temporalio.api.workflowservice.v1.UpdateActivityExecutionOptionsResponse(
            activity_options=temporalio.api.activity.v1.ActivityOptions()
        )
    )
    return service


@pytest.fixture
def interceptor() -> TracingClientInterceptor:
    return TracingClientInterceptor()


@pytest.fixture
def client(interceptor: TracingClientInterceptor) -> Client:
    service_client = Mock(spec=ServiceClient)
    service_client.workflow_service = _stub_service()
    service_client.config = Mock(identity="test-identity")
    return Client(
        service_client=service_client,
        namespace="test-namespace",
        interceptors=[interceptor],
    )


async def test_interceptor_invokes_each_operator_command(
    client: Client, interceptor: TracingClientInterceptor
):
    handle = client.get_activity_handle("act-1", run_id="run-1")
    await handle.pause(reason="pause-reason")
    await handle.unpause(reason="unpause-reason", jitter=timedelta(seconds=5))
    await handle.update_options(
        [ActivityOptionsKeys.start_to_close_timeout.value_set(timedelta(seconds=90))]
    )
    await handle.restore_original_options()

    assert [name for name, _ in interceptor.traces] == [
        "pause_activity",
        "unpause_activity",
        "update_activity_options",
        "update_activity_options",
    ]

    for name, input in interceptor.traces:
        assert input.activity_id == "act-1", name
        assert input.activity_run_id == "run-1", name


async def test_interceptor_receives_command_arguments(
    client: Client, interceptor: TracingClientInterceptor
):
    handle = client.get_activity_handle("act-1")
    await handle.pause(reason="pause-reason")
    await handle.unpause(reason="unpause-reason", jitter=timedelta(seconds=5))

    traces = dict(interceptor.traces)
    assert traces["pause_activity"].reason == "pause-reason"
    assert traces["unpause_activity"].reason == "unpause-reason"
    assert traces["unpause_activity"].jitter == timedelta(seconds=5)
