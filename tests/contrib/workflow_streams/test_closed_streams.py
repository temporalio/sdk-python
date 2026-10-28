"""Closed-run stream reads and subscription run isolation."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from temporalio import workflow
from temporalio.client import (
    Client,
    WorkflowExecutionStatus,
    WorkflowHandle,
    WorkflowQueryFailedError,
    WorkflowQueryRejectedError,
    WorkflowUpdateFailedError,
    WorkflowUpdateRPCTimeoutOrCancelledError,
)
from temporalio.common import QueryRejectCondition, RawValue
from temporalio.contrib.workflow_streams import (
    PollInput,
    PollResult,
    WorkflowStream,
    WorkflowStreamClient,
)
from temporalio.contrib.workflow_streams._types import (
    _decode_payload,
    _WorkflowStreamWireItem,
)
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode
from tests.helpers import assert_eq_eventually, new_worker

POLL = "__temporal_workflow_stream_poll"
WIRE_ABC = "ChgKCGVuY29kaW5nEgxiaW5hcnkvcGxhaW4SA2FiYw=="
TERMINAL_STATUSES = [
    WorkflowExecutionStatus.COMPLETED,
    WorkflowExecutionStatus.FAILED,
    WorkflowExecutionStatus.CANCELED,
    WorkflowExecutionStatus.TERMINATED,
    WorkflowExecutionStatus.TIMED_OUT,
]


def mock_stream(
    status: WorkflowExecutionStatus = WorkflowExecutionStatus.COMPLETED,
) -> tuple[WorkflowStreamClient, Any, Any, Any]:
    client = MagicMock(spec=Client)
    client.data_converter = DataConverter.default
    poll = MagicMock(spec=WorkflowHandle)
    poll.id = "workflow"
    poll._client = client
    poll.start_update = AsyncMock(
        side_effect=RPCError("closed", RPCStatusCode.NOT_FOUND, b"")
    )
    poll.describe = AsyncMock(
        return_value=SimpleNamespace(status=status, run_id="closed-run")
    )
    read = MagicMock(spec=WorkflowHandle)
    read.describe = poll.describe
    read.query = AsyncMock(return_value=PollResult())
    client.get_workflow_handle.return_value = read
    return WorkflowStreamClient(poll, client=client), client, poll, read


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
async def test_workflow_streams_terminal_pages(status: WorkflowExecutionStatus) -> None:
    stream, client, poll, read = mock_stream(status)
    read.query.side_effect = [
        PollResult([_WorkflowStreamWireItem("events", WIRE_ABC, 3)], 4, True),
        PollResult([_WorkflowStreamWireItem("events", WIRE_ABC, 5)], 6, False),
    ]
    items = [item async for item in stream.subscribe("events", 3, result_type=bytes)]
    assert [(item.offset, item.data) for item in items] == [(3, b"abc"), (5, b"abc")]
    client.get_workflow_handle.assert_called_once_with("workflow", run_id="closed-run")
    assert poll.start_update.await_count == 1
    assert [call.args for call in read.query.await_args_list] == [
        (POLL, PollInput(["events"], 3)),
        (POLL, PollInput(["events"], 4)),
    ]


async def test_workflow_streams_direct_handle_keeps_default_converter() -> None:
    _, client, poll, read = mock_stream()
    read.query.return_value = PollResult(
        [_WorkflowStreamWireItem("events", WIRE_ABC, 0)], 1
    )
    stream = WorkflowStreamClient(poll)
    items = [item async for item in stream.subscribe(result_type=RawValue)]
    assert items[0].data.payload.data == b"abc"
    client.get_workflow_handle.assert_called_once_with("workflow", run_id="closed-run")


async def test_workflow_streams_update_to_query_resumes_at_next_offset() -> None:
    stream, client, poll, read = mock_stream()
    poll.start_update.side_effect = [
        SimpleNamespace(
            workflow_run_id="closed-run",
            result=AsyncMock(
                return_value=PollResult(
                    [_WorkflowStreamWireItem("events", WIRE_ABC, i) for i in (0, 1)],
                    2,
                    True,
                )
            ),
        ),
        SimpleNamespace(
            workflow_run_id="closed-run",
            result=AsyncMock(
                return_value=PollResult(
                    [_WorkflowStreamWireItem("events", WIRE_ABC, 2)], 3
                )
            ),
        ),
        SimpleNamespace(
            workflow_run_id="closed-run",
            result=AsyncMock(
                side_effect=WorkflowUpdateFailedError(
                    ApplicationError("closed", type="AcceptedUpdateCompletedWorkflow")
                )
            ),
        ),
    ]
    read.query.side_effect = [
        PollResult([_WorkflowStreamWireItem("events", WIRE_ABC, 3)], 4, True),
        PollResult([_WorkflowStreamWireItem("events", WIRE_ABC, 4)], 5),
    ]
    items = [
        item
        async for item in stream.subscribe(
            "events", result_type=bytes, poll_cooldown=timedelta(0)
        )
    ]
    assert [(item.offset, item.data) for item in items] == [
        (i, b"abc") for i in range(5)
    ]
    assert [call.args for call in poll.start_update.await_args_list] == [
        (POLL, PollInput(["events"], offset)) for offset in (0, 2, 3)
    ]
    assert [call.args for call in read.query.await_args_list] == [
        (POLL, PollInput(["events"], offset)) for offset in (3, 4)
    ]
    read.describe.assert_awaited_once()
    assert client.get_workflow_handle.call_count == 2
    for call in client.get_workflow_handle.call_args_list:
        assert call.args == ("workflow",)
        assert call.kwargs == {"run_id": "closed-run"}


@pytest.mark.parametrize(
    "failure", ["running", "unknown", "missing_run", "describe", "query"]
)
async def test_workflow_streams_unconfirmed_closure_and_query_errors(
    failure: str,
) -> None:
    stream, _, poll, read = mock_stream()
    expected: Exception
    if failure in ("running", "unknown", "missing_run"):
        if failure == "missing_run":
            poll.describe.return_value.run_id = ""
        else:
            poll.describe.return_value.status = (
                WorkflowExecutionStatus.RUNNING if failure == "running" else None
            )
        expected = poll.start_update.side_effect
    elif failure == "describe":
        poll.describe.side_effect = RPCError("missing", RPCStatusCode.NOT_FOUND, b"")
        expected = poll.start_update.side_effect
    else:
        expected = WorkflowQueryFailedError("missing handler or query rejected")
        read.query.side_effect = expected
    with pytest.raises(type(expected)) as err:
        _ = [item async for item in stream.subscribe()]
    assert err.value is expected


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("failure", ["rpc", "update", "timeout"])
async def test_workflow_streams_describe_failure_preserves_poll_error(
    accepted: bool, failure: str
) -> None:
    stream, _, poll, read = mock_stream()
    expected: Exception
    if failure == "rpc":
        expected = RPCError("poll failed", RPCStatusCode.UNAVAILABLE, b"")
    elif failure == "update":
        expected = WorkflowUpdateFailedError(ApplicationError("poll failed"))
    else:
        expected = WorkflowUpdateRPCTimeoutOrCancelledError()
    cause = expected.__cause__
    poll.describe.side_effect = RPCError(
        "describe failed", RPCStatusCode.NOT_FOUND, b""
    )
    if accepted:
        poll.start_update.side_effect = None
        poll.start_update.return_value = SimpleNamespace(
            workflow_run_id="closed-run", result=AsyncMock(side_effect=expected)
        )
    else:
        poll.start_update.side_effect = expected
    with pytest.raises(type(expected)) as err:
        _ = [item async for item in stream.subscribe()]
    assert err.value is expected
    assert err.value.__cause__ is cause
    read.query.assert_not_awaited()


async def test_workflow_streams_describe_cancellation_propagates() -> None:
    stream, _, poll, read = mock_stream()
    poll.describe.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        _ = [item async for item in stream.subscribe()]
    read.query.assert_not_awaited()


async def test_workflow_streams_run_tracking_is_local_to_each_attempt() -> None:
    stream, client, poll, _ = mock_stream()
    admitted: asyncio.Queue[str] = asyncio.Queue()
    outcomes: dict[str, asyncio.Future[PollResult]] = {}
    reads: dict[str, Any] = {}

    async def start_update(*_args: Any, **_kwargs: Any) -> Any:
        run_id = f"run-{len(outcomes)}"
        future: asyncio.Future[PollResult] = asyncio.get_running_loop().create_future()
        outcomes[run_id] = future
        admitted.put_nowait(run_id)
        return SimpleNamespace(workflow_run_id=run_id, result=lambda: future)

    def pinned_handle(workflow_id: str, *, run_id: str | None = None) -> Any:
        assert workflow_id == "workflow"
        assert run_id is not None
        if run_id not in reads:
            read = MagicMock(spec=WorkflowHandle)
            read.describe = AsyncMock(
                return_value=SimpleNamespace(
                    status=WorkflowExecutionStatus.COMPLETED, run_id=run_id
                )
            )
            read.query = AsyncMock(
                return_value=PollResult(
                    [_WorkflowStreamWireItem(run_id, WIRE_ABC, 0)], 1
                )
            )
            reads[run_id] = read
        return reads[run_id]

    poll.start_update.side_effect = start_update
    client.get_workflow_handle.side_effect = pinned_handle

    async def consume() -> list[str]:
        return [item.topic async for item in stream.subscribe()]

    first = asyncio.create_task(consume())
    assert await admitted.get() == "run-0"
    second = asyncio.create_task(consume())
    assert await admitted.get() == "run-1"
    for run_id in ["run-1", "run-0"]:
        outcomes[run_id].set_exception(
            WorkflowUpdateFailedError(
                ApplicationError("closed", type="AcceptedUpdateCompletedWorkflow")
            )
        )
    assert await first == ["run-0"]
    assert await second == ["run-1"]


async def test_workflow_streams_continue_as_new_resets_accepted_run() -> None:
    stream, client, poll, read = mock_stream()
    accepted = SimpleNamespace(
        workflow_run_id="old-run",
        result=AsyncMock(
            side_effect=WorkflowUpdateFailedError(
                ApplicationError("closed", type="AcceptedUpdateCompletedWorkflow")
            )
        ),
    )
    poll.start_update.side_effect = None
    poll.start_update.return_value = accepted
    old = MagicMock(spec=WorkflowHandle)
    old.describe = AsyncMock(
        return_value=SimpleNamespace(
            status=WorkflowExecutionStatus.CONTINUED_AS_NEW, run_id="old-run"
        )
    )
    successor = MagicMock(spec=WorkflowHandle)
    successor._client = client
    successor.start_update = AsyncMock(
        side_effect=RPCError("closed", RPCStatusCode.NOT_FOUND, b"")
    )
    successor.describe = AsyncMock(
        return_value=SimpleNamespace(
            status=WorkflowExecutionStatus.COMPLETED, run_id="successor-run"
        )
    )
    read.query.return_value = PollResult(
        [_WorkflowStreamWireItem("events", WIRE_ABC, 0)], 1
    )

    def get_handle(workflow_id: str, *, run_id: str | None = None) -> Any:
        assert workflow_id == "workflow"
        return old if run_id == "old-run" else successor if run_id is None else read

    client.get_workflow_handle.side_effect = get_handle
    assert [item.data async for item in stream.subscribe()] == [b"abc"]
    old.describe.assert_awaited_once()
    successor.describe.assert_awaited_once()
    assert client.get_workflow_handle.call_args_list[-1].kwargs == {
        "run_id": "successor-run"
    }


async def test_workflow_streams_shared_wire_vector() -> None:
    converter = DataConverter.default
    request = '{"topics":["events"],"from_offset":3}'
    response = (
        '{"items":[{"topic":"events","data":"'
        + WIRE_ABC
        + '","offset":3}],"next_offset":4,"more_ready":false}'
    )
    for value, expected in [
        (PollInput(["events"], 3), request),
        (PollResult([_WorkflowStreamWireItem("events", WIRE_ABC, 3)], 4), response),
    ]:
        payloads = await converter.encode([value])
        assert json.loads(payloads[0].data) == json.loads(expected)
        decoded = (await converter.decode(payloads, [type(value)]))[0]
        assert decoded == value
    assert _decode_payload(WIRE_ABC).data == b"abc"
    assert _decode_payload(WIRE_ABC).metadata == {"encoding": b"binary/plain"}


@dataclass
class ClosedStreamInput:
    count: int = 3
    size: int = 1
    truncate: int = 0
    fail: bool = False
    tail: bool = False


@workflow.defn
class ClosedStreamWorkflow:
    @workflow.init
    def __init__(self, arg: ClosedStreamInput) -> None:
        self.stream = WorkflowStream()
        self.closed = False

    @workflow.signal
    def close(self) -> None:
        self.closed = True

    @workflow.signal
    def detach(self) -> None:
        self.stream.detach_pollers()

    @workflow.run
    async def run(self, arg: ClosedStreamInput) -> None:
        for i in range(arg.count):
            self.stream.topic("events" if i % 2 == 0 else "other").publish(
                bytes([i]) * arg.size
            )
        self.stream.truncate(arg.truncate)
        await workflow.wait_condition(lambda: self.closed)
        if arg.tail:
            self.stream.topic("events").publish(b"tail")
        if arg.fail:
            raise ApplicationError("closed stream failure")


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
async def test_workflow_streams_read_after_terminal_cold_replay(
    client: Client, status: WorkflowExecutionStatus
) -> None:
    async with new_worker(
        client, ClosedStreamWorkflow, max_cached_workflows=0
    ) as worker:
        handle = await client.start_workflow(
            ClosedStreamWorkflow.run,
            ClosedStreamInput(fail=status == WorkflowExecutionStatus.FAILED),
            id=f"closed-stream-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            run_timeout=timedelta(seconds=2)
            if status == WorkflowExecutionStatus.TIMED_OUT
            else None,
        )
        stream = WorkflowStreamClient(handle)
        await assert_eq_eventually(3, stream.get_offset)
        if status == WorkflowExecutionStatus.CANCELED:
            await handle.cancel()
        elif status == WorkflowExecutionStatus.TERMINATED:
            await handle.terminate()
        elif status != WorkflowExecutionStatus.TIMED_OUT:
            await handle.signal(ClosedStreamWorkflow.close)

        async def execution_status() -> WorkflowExecutionStatus:
            status = (await handle.describe()).status
            assert status is not None
            return status

        await assert_eq_eventually(status, execution_status)
        before = await handle.fetch_history()
        items = [item async for item in stream.subscribe(result_type=bytes)]
        assert [item.data for item in items] == [b"\x00", b"\x01", b"\x02"]
        assert [item.offset for item in items] == [0, 1, 2]
        assert (await handle.fetch_history()).events == before.events


@pytest.mark.parametrize(
    "arg,topics,offset,expected",
    [
        (ClosedStreamInput(count=0), None, 0, []),
        (ClosedStreamInput(), ["missing"], 0, []),
        (ClosedStreamInput(), None, 1, [1, 2]),
        (ClosedStreamInput(), None, 3, []),
        (ClosedStreamInput(), None, 10, []),
        (ClosedStreamInput(count=6, truncate=3), None, 1, [3, 4, 5]),
        (ClosedStreamInput(count=8, size=200_000), None, 0, list(range(8))),
        (ClosedStreamInput(count=8, size=200_000), ["events"], 0, [0, 2, 4, 6]),
    ],
)
async def test_workflow_streams_closed_retention_and_pagination(
    client: Client,
    arg: ClosedStreamInput,
    topics: list[str] | None,
    offset: int,
    expected: list[int],
) -> None:
    async with new_worker(client, ClosedStreamWorkflow) as worker:
        handle = await client.start_workflow(
            ClosedStreamWorkflow.run,
            arg,
            id=f"closed-stream-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.signal(ClosedStreamWorkflow.close)
        await handle.result()
        stream = WorkflowStreamClient.create(client, handle.id)
        items = [
            item async for item in stream.subscribe(topics, offset, result_type=bytes)
        ]
        assert [item.offset for item in items] == expected
        assert [item.data for item in items] == [
            bytes([i]) * arg.size for i in expected
        ]


@pytest.mark.filterwarnings(
    "ignore::temporalio.workflow.UnfinishedUpdateHandlersWarning"
)
async def test_workflow_streams_accepted_poll_drains_unread_tail(
    client: Client,
) -> None:
    async with new_worker(client, ClosedStreamWorkflow) as worker:
        handle = await client.start_workflow(
            ClosedStreamWorkflow.run,
            ClosedStreamInput(tail=True),
            id=f"closed-stream-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        stream = WorkflowStreamClient(handle)
        await assert_eq_eventually(3, stream.get_offset)
        accepted = asyncio.Event()
        original_start = handle.start_update

        async def start(*args: Any, **kwargs: Any) -> Any:
            update = await original_start(*args, **kwargs)
            if args[1].from_offset == 3:
                accepted.set()
            return update

        async def consume() -> list[tuple[int, bytes]]:
            return [
                (item.offset, item.data)
                async for item in stream.subscribe(
                    result_type=bytes, poll_cooldown=timedelta(0)
                )
            ]

        with patch.object(handle, "start_update", side_effect=start):
            task = asyncio.create_task(consume())
            await accepted.wait()
            await handle.signal(ClosedStreamWorkflow.close)
            assert await task == [
                (0, b"\x00"),
                (1, b"\x01"),
                (2, b"\x02"),
                (3, b"tail"),
            ]


async def test_workflow_streams_query_is_immediate_while_draining(
    client: Client,
) -> None:
    async with new_worker(client, ClosedStreamWorkflow) as worker:
        handle = await client.start_workflow(
            ClosedStreamWorkflow.run,
            ClosedStreamInput(count=6, truncate=3),
            id=f"closed-stream-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.signal(ClosedStreamWorkflow.detach)
        result = await handle.query(POLL, PollInput([], 1), result_type=PollResult)
        assert [item.offset for item in result.items] == [3, 4, 5]
        empty = await handle.query(
            POLL, PollInput(["missing"], 6), result_type=PollResult
        )
        assert empty == PollResult([], 6)
        await handle.signal(ClosedStreamWorkflow.close)
        await handle.result()


async def test_workflow_streams_closed_codec_and_raw_conversion(client: Client) -> None:
    from tests.test_converter import SimpleCodec

    config = client.config()
    config["data_converter"] = DataConverter(payload_codec=SimpleCodec())
    codec_client = Client(**config)
    async with new_worker(
        codec_client, ClosedStreamWorkflow, max_cached_workflows=0
    ) as worker:
        handle = await codec_client.start_workflow(
            ClosedStreamWorkflow.run,
            ClosedStreamInput(),
            id=f"closed-stream-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.signal(ClosedStreamWorkflow.close)
        await handle.result()
        stream = WorkflowStreamClient.create(codec_client, handle.id)
        typed = [item.data async for item in stream.subscribe(result_type=bytes)]
        raw = [
            item.data.payload.data
            async for item in stream.subscribe(result_type=RawValue)
        ]
        assert typed == raw == [b"\x00", b"\x01", b"\x02"]


async def test_workflow_streams_closed_explicit_run_after_workflow_id_reuse(
    client: Client,
) -> None:
    async with new_worker(client, ClosedStreamWorkflow) as worker:
        workflow_id = f"closed-stream-{uuid.uuid4()}"
        old = await client.start_workflow(
            ClosedStreamWorkflow.run,
            ClosedStreamInput(),
            id=workflow_id,
            task_queue=worker.task_queue,
        )
        await old.signal(ClosedStreamWorkflow.close)
        await old.result()
        new = await client.start_workflow(
            ClosedStreamWorkflow.run,
            ClosedStreamInput(count=1),
            id=workflow_id,
            task_queue=worker.task_queue,
        )
        pinned = WorkflowStreamClient(
            client.get_workflow_handle(workflow_id, run_id=old.result_run_id)
        )

        async def consume() -> list[int]:
            return [item.offset async for item in pinned.subscribe()]

        assert await asyncio.gather(consume(), consume()) == [[0, 1, 2], [0, 1, 2]]
        await new.signal(ClosedStreamWorkflow.close)
        await new.result()
        latest = WorkflowStreamClient.create(client, workflow_id)
        assert [item.offset async for item in latest.subscribe()] == [0]


@workflow.defn
class LegacyClosedStreamWorkflow:
    @workflow.run
    async def run(self) -> None:
        pass


async def test_workflow_streams_missing_query_and_configured_query_rejection(
    client: Client,
) -> None:
    async with new_worker(
        client, ClosedStreamWorkflow, LegacyClosedStreamWorkflow
    ) as worker:
        legacy = await client.start_workflow(
            LegacyClosedStreamWorkflow.run,
            id=f"closed-stream-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await legacy.result()
        with pytest.raises(WorkflowQueryFailedError):
            _ = [item async for item in WorkflowStreamClient(legacy).subscribe()]

        handle = await client.start_workflow(
            ClosedStreamWorkflow.run,
            ClosedStreamInput(),
            id=f"closed-stream-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.signal(ClosedStreamWorkflow.close)
        await handle.result()
        config = client.config()
        config["default_workflow_query_reject_condition"] = (
            QueryRejectCondition.NOT_OPEN
        )
        stream = WorkflowStreamClient.create(Client(**config), handle.id)
        with pytest.raises(WorkflowQueryRejectedError):
            _ = [item async for item in stream.subscribe()]

    missing = WorkflowStreamClient.create(client, f"missing-{uuid.uuid4()}")
    with pytest.raises(RPCError) as err:
        _ = [item async for item in missing.subscribe()]
    assert err.value.status == RPCStatusCode.NOT_FOUND
