"""Unit tests for the operator-command request fields the server does not surface back."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

import temporalio.api.activity.v1
import temporalio.api.workflowservice.v1
from temporalio.client import ActivityOptionsKeys, Client
from temporalio.service import ServiceClient


class _CapturedService:
    """Capture operator command requests."""

    def __init__(self) -> None:
        self.requests: dict[str, Any] = {}
        self.pause_activity_execution = self._recorder(
            "pause",
            temporalio.api.workflowservice.v1.PauseActivityExecutionResponse(),
        )
        self.unpause_activity_execution = self._recorder(
            "unpause",
            temporalio.api.workflowservice.v1.UnpauseActivityExecutionResponse(),
        )
        self.reset_activity_execution = self._recorder(
            "reset",
            temporalio.api.workflowservice.v1.ResetActivityExecutionResponse(),
        )
        self.update_activity_execution_options = self._recorder(
            "update",
            temporalio.api.workflowservice.v1.UpdateActivityExecutionOptionsResponse(
                activity_options=temporalio.api.activity.v1.ActivityOptions()
            ),
        )

    def _recorder(self, name: str, response: Any) -> AsyncMock:
        async def record(req: Any, **_kwargs: Any) -> Any:
            self.requests[name] = req
            return response

        return AsyncMock(side_effect=record)


@pytest.fixture
def captured() -> _CapturedService:
    return _CapturedService()


@pytest.fixture
def client(captured: _CapturedService) -> Client:
    service_client = Mock(spec=ServiceClient)
    service_client.workflow_service = captured
    service_client.config = Mock(identity="test-identity")
    return Client(service_client=service_client, namespace="test-namespace")


async def test_operator_commands_send_identity_and_request_id(
    client: Client, captured: _CapturedService
):
    handle = client.get_activity_handle("act-1", run_id="run-1")
    await handle.pause(reason="pause-reason")
    await handle.unpause(reason="unpause-reason", jitter=timedelta(seconds=5))
    await handle.reset(jitter=timedelta(seconds=7))
    await handle.update_options(
        [ActivityOptionsKeys.heartbeat_timeout.value_set(timedelta(seconds=25))]
    )

    assert set(captured.requests) == {"pause", "unpause", "reset", "update"}
    request_ids = set()
    for name, req in captured.requests.items():
        assert req.namespace == "test-namespace", name
        assert req.activity_id == "act-1", name
        assert req.run_id == "run-1", name
        assert req.identity == "test-identity", name
        assert req.request_id, name
        request_ids.add(req.request_id)

    # Request IDs must be fresh per call, not reused across commands.
    assert len(request_ids) == 4


async def test_operator_commands_send_reason_and_jitter(
    client: Client, captured: _CapturedService
):
    handle = client.get_activity_handle("act-1")
    await handle.pause(reason="pause-reason")
    await handle.unpause(reason="unpause-reason", jitter=timedelta(seconds=5))
    await handle.reset(jitter=timedelta(seconds=7))

    assert captured.requests["pause"].reason == "pause-reason"
    assert captured.requests["unpause"].reason == "unpause-reason"
    assert captured.requests["unpause"].jitter.ToTimedelta() == timedelta(seconds=5)
    assert captured.requests["reset"].jitter.ToTimedelta() == timedelta(seconds=7)


async def test_omitted_jitter_is_left_off_the_wire(
    client: Client, captured: _CapturedService
):
    # An unset jitter must not be sent as an explicit zero duration, or the server would
    # apply "no jitter" instead of its own default.
    handle = client.get_activity_handle("act-1")
    await handle.unpause()
    await handle.reset()

    assert not captured.requests["unpause"].HasField("jitter")
    assert not captured.requests["reset"].HasField("jitter")


async def test_reset_flags_default_off(client: Client, captured: _CapturedService):
    handle = client.get_activity_handle("act-1")
    await handle.reset()

    reset = captured.requests["reset"]
    assert not reset.keep_paused
    assert not reset.restore_original_options
    assert not reset.reset_heartbeat


async def test_reset_flags_reach_the_request(
    client: Client, captured: _CapturedService
):
    handle = client.get_activity_handle("act-1")
    await handle.reset(
        keep_paused=True, restore_original_options=True, reset_heartbeat=True
    )

    reset = captured.requests["reset"]
    assert reset.keep_paused
    assert reset.restore_original_options
    assert reset.reset_heartbeat


async def test_restore_original_options_is_exclusive(
    client: Client, captured: _CapturedService
):
    handle = client.get_activity_handle("act-1")
    await handle.restore_original_options()

    update = captured.requests["update"]
    assert update.restore_original
    assert list(update.update_mask.paths) == []


async def test_value_set_of_zero_sends_an_explicit_zero(
    client: Client, captured: _CapturedService
):
    handle = client.get_activity_handle("act-1")
    await handle.update_options(
        [ActivityOptionsKeys.heartbeat_timeout.value_set(timedelta(0))]
    )

    update = captured.requests["update"]
    assert sorted(update.update_mask.paths) == ["heartbeat_timeout"]
    # Present and zero, which is distinct from absent: the caller asked for zero.
    assert update.activity_options.HasField("heartbeat_timeout")
    assert update.activity_options.heartbeat_timeout.ToTimedelta() == timedelta(0)


async def test_value_unset_names_the_path_but_leaves_the_field_absent(
    client: Client, captured: _CapturedService
):
    handle = client.get_activity_handle("act-1")
    await handle.update_options([ActivityOptionsKeys.heartbeat_timeout.value_unset()])

    update = captured.requests["update"]
    assert sorted(update.update_mask.paths) == ["heartbeat_timeout"]
    # Absent, which is how the server is told to clear the option.
    assert not update.activity_options.HasField("heartbeat_timeout")


async def test_a_repeated_key_resolves_to_its_last_update(
    client: Client, captured: _CapturedService
):
    handle = client.get_activity_handle("act-1")
    await handle.update_options(
        [
            ActivityOptionsKeys.heartbeat_timeout.value_set(timedelta(seconds=5)),
            ActivityOptionsKeys.heartbeat_timeout.value_unset(),
        ]
    )

    update = captured.requests["update"]
    # The later unset wins, and the path is named once.
    assert sorted(update.update_mask.paths) == ["heartbeat_timeout"]
    assert not update.activity_options.HasField("heartbeat_timeout")


async def test_update_options_requires_at_least_one_update(client: Client):
    handle = client.get_activity_handle("act-1")
    with pytest.raises(ValueError) as err:
        await handle.update_options([])
    assert "at least one update" in str(err.value)


async def test_restore_original_cannot_be_combined_with_updates(client: Client):
    from temporalio.client import UpdateActivityOptionsInput

    with pytest.raises(ValueError) as err:
        await client._impl.update_activity_options(
            UpdateActivityOptionsInput(
                activity_id="act-1",
                activity_run_id=None,
                updates=[
                    ActivityOptionsKeys.heartbeat_timeout.value_set(
                        timedelta(seconds=25)
                    )
                ],
                restore_original=True,
                rpc_metadata={},
                rpc_timeout=None,
            )
        )
    assert "cannot be combined" in str(err.value)


async def test_update_options_masks_only_changed_options(
    client: Client, captured: _CapturedService
):
    handle = client.get_activity_handle("act-1")
    await handle.update_options(
        [
            ActivityOptionsKeys.task_queue.value_set("tq"),
            ActivityOptionsKeys.start_to_close_timeout.value_set(timedelta(seconds=90)),
        ]
    )

    update = captured.requests["update"]
    assert not update.restore_original
    assert sorted(update.update_mask.paths) == [
        "start_to_close_timeout",
        "task_queue.name",
    ]
    assert update.activity_options.task_queue.name == "tq"
    assert update.activity_options.start_to_close_timeout.ToTimedelta() == timedelta(
        seconds=90
    )
