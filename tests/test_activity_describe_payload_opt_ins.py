"""Unit tests for the opt-in describe payload fields (api#792).

Two things that a live server cannot demonstrate on its own: that the flags the caller
set actually reach the request, and that payloads a server returns without being asked
are dropped client-side so the has_* accessors always agree with the request.
"""

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


def _payloads() -> temporalio.api.common.v1.Payloads:
    return temporalio.api.common.v1.Payloads(
        payloads=[
            temporalio.api.common.v1.Payload(
                metadata={"encoding": b"json/plain"}, data=b'"x"'
            )
        ]
    )


def _over_sharing_response() -> (
    temporalio.api.workflowservice.v1.DescribeActivityExecutionResponse
):
    """A response carrying every payload field, as an older or buggy server might send."""
    return temporalio.api.workflowservice.v1.DescribeActivityExecutionResponse(
        info=temporalio.api.activity.v1.ActivityExecutionInfo(
            activity_id="act-1",
            activity_type=temporalio.api.common.v1.ActivityType(name="my-activity"),
            heartbeat_details=_payloads(),
            last_failure=temporalio.api.failure.v1.Failure(message="last-boom"),
        ),
        input=_payloads(),
        outcome=temporalio.api.activity.v1.ActivityExecutionOutcome(result=_payloads()),
    )


class _CapturedDescribe:
    def __init__(self) -> None:
        self.request: Any = None

        async def record(req: Any, **_kwargs: Any) -> Any:
            self.request = req
            return _over_sharing_response()

        self.describe_activity_execution = AsyncMock(side_effect=record)


@pytest.fixture
def captured() -> _CapturedDescribe:
    return _CapturedDescribe()


@pytest.fixture
def client(captured: _CapturedDescribe) -> Client:
    service_client = Mock(spec=ServiceClient)
    service_client.workflow_service = captured
    service_client.config = Mock(identity="test-identity")
    return Client(service_client=service_client, namespace="test-namespace")


async def test_describe_defaults_ask_for_nothing(
    client: Client, captured: _CapturedDescribe
):
    await client.get_activity_handle("act-1").describe()

    assert not captured.request.include_input
    assert not captured.request.include_outcome
    assert not captured.request.include_heartbeat_details
    assert not captured.request.include_last_failure


async def test_describe_forwards_each_flag(client: Client, captured: _CapturedDescribe):
    await client.get_activity_handle("act-1").describe(
        include_input=True,
        include_outcome=True,
        include_heartbeat_details=True,
        include_last_failure=True,
    )

    assert captured.request.include_input
    assert captured.request.include_outcome
    assert captured.request.include_heartbeat_details
    assert captured.request.include_last_failure


async def test_describe_flags_are_independent(
    client: Client, captured: _CapturedDescribe
):
    await client.get_activity_handle("act-1").describe(include_input=True)

    assert captured.request.include_input
    assert not captured.request.include_outcome
    assert not captured.request.include_heartbeat_details
    assert not captured.request.include_last_failure


async def test_unrequested_payloads_are_stripped(client: Client):
    # The stub returns every payload field regardless of what was asked for.
    desc = await client.get_activity_handle("act-1").describe()

    assert not desc.has_input
    assert desc.input is None
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

    assert desc.has_input
    assert desc.input == ["x"]
    assert desc.has_result
    assert desc.result == "x"
    assert len(desc.raw_heartbeat_details) == 1
    assert desc.last_failure is not None


async def test_stripping_is_per_field(client: Client):
    # Asking for one payload must not let the others through.
    desc = await client.get_activity_handle("act-1").describe(include_input=True)

    assert desc.has_input
    assert not desc.has_result
    assert len(desc.raw_heartbeat_details) == 0
    assert desc.last_failure is None
