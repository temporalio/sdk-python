"""Unit tests for the opt-in describe payload fields."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

import temporalio.api.activity.v1
import temporalio.api.common.v1
import temporalio.api.failure.v1
import temporalio.api.workflowservice.v1
from temporalio.client import Client
from temporalio.service import ServiceClient


def _payloads(value: str) -> temporalio.api.common.v1.Payloads:
    return temporalio.api.common.v1.Payloads(
        payloads=[
            temporalio.api.common.v1.Payload(
                metadata={"encoding": b"json/plain"}, data=f'"{value}"'.encode()
            )
        ]
    )


def _unrequested_fields_response() -> (
    temporalio.api.workflowservice.v1.DescribeActivityExecutionResponse
):
    """A response carrying every payload field, as an older or buggy server might send."""
    return temporalio.api.workflowservice.v1.DescribeActivityExecutionResponse(
        info=temporalio.api.activity.v1.ActivityExecutionInfo(
            activity_id="act-1",
            activity_type=temporalio.api.common.v1.ActivityType(name="my-activity"),
            heartbeat_details=_payloads("hb"),
            last_failure=temporalio.api.failure.v1.Failure(message="last-boom"),
        ),
        input=_payloads("in"),
        outcome=temporalio.api.activity.v1.ActivityExecutionOutcome(
            result=_payloads("out")
        ),
    )


class _DescribeReturnsUnrequestedPayloads:
    """Stands in for the raw gRPC workflow service, always returning all payloads."""

    def __init__(self) -> None:
        async def respond(_req: Any, **_kwargs: Any) -> Any:
            return _unrequested_fields_response()

        self.describe_activity_execution = AsyncMock(side_effect=respond)


@pytest.fixture
def captured() -> _DescribeReturnsUnrequestedPayloads:
    return _DescribeReturnsUnrequestedPayloads()


@pytest.fixture
def client(captured: _DescribeReturnsUnrequestedPayloads) -> Client:
    service_client = Mock(spec=ServiceClient)
    service_client.workflow_service = captured
    service_client.config = Mock(identity="test-identity")
    return Client(service_client=service_client, namespace="test-namespace")


async def test_description_exposes_the_whole_response(client: Client):
    desc = await client.get_activity_handle("act-1").describe(
        include_input=True, include_outcome=True
    )

    assert desc.raw_description.info.activity_id == "act-1"

    assert desc.input == ["in"]
    assert [p.data for p in desc.raw_description.input.payloads] == [b'"in"']

    assert desc.has_result
    assert desc.result == "out"
    assert desc.raw_description.outcome.HasField("result")
    assert [p.data for p in desc.raw_description.outcome.result.payloads] == [b'"out"']


async def test_unrequested_payloads_are_stripped(client: Client):
    # The stub returns every payload field regardless of what was asked for.
    desc = await client.get_activity_handle("act-1").describe()

    assert len(desc.raw_description.input.payloads) == 0
    assert desc.input == []
    assert not desc.has_result
    assert desc.result is None
    assert desc.failure is None
    assert len(desc.raw_heartbeat_details) == 0
    assert desc.last_failure is None


async def test_requested_payloads_are_kept(client: Client):
    desc = await client.get_activity_handle("act-1").describe(
        include_input=True,
        include_outcome=True,
        include_heartbeat_details=True,
        include_last_failure=True,
    )

    assert desc.raw_description.input.payloads
    assert desc.input == ["in"]
    assert desc.has_result
    assert desc.result == "out"
    assert len(desc.raw_heartbeat_details) == 1
    assert desc.last_failure is not None


async def test_stripping_is_per_field(client: Client):
    # Asking for one payload must not let the others through.
    desc = await client.get_activity_handle("act-1").describe(include_input=True)

    assert desc.raw_description.input.payloads
    assert not desc.has_result
    assert len(desc.raw_heartbeat_details) == 0
    assert desc.last_failure is None
