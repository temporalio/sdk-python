"""Unit tests for ``temporalio.streams``.

Each test maps to a spec invariant in
``../../native-streams/spec/StreamCommit.tla``.  Where the spec uses an
action name (e.g. ``PreparePublish``), the corresponding test comments
cite it.
"""

from __future__ import annotations

import asyncio

import pytest

from temporalio import streams
from temporalio.streams import _transport


@pytest.fixture
def transport() -> streams.FakeTransport:
    return streams.FakeTransport()


@pytest.fixture
def client(transport: streams.FakeTransport) -> streams.StreamClient:
    return streams.StreamClient(transport, namespace="ns-1")


# Spec correspondence: full PreparePublish + CommitPublish cycle through
# the SDK surface; head advances after publish.
async def test_create_and_publish_advances_head(client: streams.StreamClient):
    stream = await client.create_stream("stream-1", created_by="test-user")
    result = await stream.publish(
        [b"hello", b"world"], publisher_id="pub-1", sequence=1
    )
    assert result.first_offset == 0
    assert result.item_count == 2

    desc = await stream.describe()
    assert desc.state.head_offset == 2
    assert desc.state.base_offset == 0
    assert desc.publisher_count == 1


# Spec correspondence: dedup-replay short-circuit returns prior outcome.
async def test_publish_dedup_replay_returns_prior_outcome(
    client: streams.StreamClient, transport: streams.FakeTransport
):
    stream = await client.create_stream("stream-1")
    first = await stream.publish([b"a"], publisher_id="pub-1", sequence=1)
    second = await stream.publish([b"a"], publisher_id="pub-1", sequence=1)
    assert (first.first_offset, first.item_count) == (
        second.first_offset,
        second.item_count,
    )
    desc = await stream.describe()
    assert desc.state.head_offset == 1  # not 2 — replay was dedup'd

    # Both publish calls recorded by transport (server returns the
    # prior outcome on the second one; SDK does not short-circuit).
    publish_calls = [c for c in transport.calls if c[0] == "publish"]
    assert len(publish_calls) == 2


# Spec correspondence: per-publisher monotonic sequence.  Regression
# rejected.
async def test_publish_seq_regression_rejected(client: streams.StreamClient):
    stream = await client.create_stream("stream-1")
    await stream.publish([b"a"], publisher_id="pub-1", sequence=5)
    with pytest.raises(streams.SeqRegression):
        await stream.publish([b"a"], publisher_id="pub-1", sequence=3)


# Spec correspondence: Close → StreamClosed on subsequent publish.
async def test_close_rejects_subsequent_publish(client: streams.StreamClient):
    stream = await client.create_stream("stream-1")
    await stream.close(closed_by="tester")
    with pytest.raises(streams.StreamClosed):
        await stream.publish([b"a"], publisher_id="pub-1", sequence=1)


# Spec correspondence: TruncateBeyondHead guard.
async def test_truncate_beyond_head_rejected(client: streams.StreamClient):
    stream = await client.create_stream("stream-1")
    with pytest.raises(streams.TruncateBeyondHead):
        await stream.truncate(100)


# Spec correspondence: Truncate moves base_offset; OffsetTruncated on
# subsequent read below the new base.
async def test_truncate_advances_base_and_blocks_low_reads(
    client: streams.StreamClient,
):
    stream = await client.create_stream("stream-1")
    await stream.publish(
        [b"a", b"b", b"c", b"d", b"e"], publisher_id="pub-1", sequence=1
    )

    result = await stream.truncate(3)
    assert result.new_base_offset == 3

    with pytest.raises(streams.OffsetTruncated):
        async for _ in stream.read_range(start_offset=0):
            break


# Spec correspondence: ReadRange returns items in offset order honoring
# the visibility filter (CommitedTxnID).  Caller iterates with offsets.
async def test_read_range_iterates_in_order(client: streams.StreamClient):
    stream = await client.create_stream("stream-1")
    await stream.publish([b"a", b"b", b"c"], publisher_id="pub-1", sequence=1)
    await stream.publish([b"d", b"e"], publisher_id="pub-2", sequence=1)

    got: list[tuple[int, bytes]] = []
    async for offset, item in stream.read_range(start_offset=0, end_offset=5):
        got.append((offset, item.data))
    assert got == [
        (0, b"a"),
        (1, b"b"),
        (2, b"c"),
        (3, b"d"),
        (4, b"e"),
    ]


async def test_read_range_topic_filter_preserves_offsets(
    client: streams.StreamClient,
):
    stream = await client.create_stream("stream-1")
    await stream.publish(
        [
            streams.PublishItem(data=b"a", topic="alpha"),
            streams.PublishItem(data=b"b", topic="beta"),
            streams.PublishItem(data=b"c", topic="alpha"),
        ],
        publisher_id="pub-1",
        sequence=1,
    )

    got: list[tuple[int, bytes]] = []
    async for offset, item in stream.read_range(
        start_offset=0, end_offset=3, topics=["beta"], page_size=1
    ):
        got.append((offset, item.data))
    assert got == [(1, b"b")]


# Publisher-side: the Publisher context manager flushes buffered items
# on close, and dedup behavior is preserved across batches because each
# flush uses a fresh sequence.
async def test_publisher_flushes_on_exit(client: streams.StreamClient):
    stream = await client.create_stream("stream-1")
    async with stream.publisher(publisher_id="pub-1", max_batch_items=2) as pub:
        await pub.publish(b"a")
        await pub.publish(b"b")  # triggers a flush at max_batch_items=2
        await pub.publish(b"c")  # buffered

    desc = await stream.describe()
    assert desc.state.head_offset == 3


async def test_publisher_failed_flush_retains_batch_and_sequence():
    class FailOnceTransport(streams.FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.seen_sequences: list[int] = []
            self.fail_once = True

        async def publish(
            self, req: _transport.PublishRequest
        ) -> streams.PublishResult:
            self.seen_sequences.append(req.sequence)
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("transient publish failure")
            return await super().publish(req)

    transport = FailOnceTransport()
    client = streams.StreamClient(transport, namespace="ns-1")
    stream = await client.create_stream("stream-1")
    pub = stream.publisher(publisher_id="pub-1")

    await pub.publish(b"a")
    with pytest.raises(RuntimeError):
        await pub.flush()

    desc = await stream.describe()
    assert desc.state.head_offset == 0

    result = await pub.flush()
    assert result is not None
    assert result.first_offset == 0
    assert result.item_count == 1
    assert transport.seen_sequences == [1, 1]


# Two distinct publisher IDs do NOT share a dedup-state; each can use
# sequence 1 independently.
async def test_distinct_publishers_isolated(client: streams.StreamClient):
    stream = await client.create_stream("stream-1")
    a = await stream.publish([b"a"], publisher_id="pub-1", sequence=1)
    b = await stream.publish([b"b"], publisher_id="pub-2", sequence=1)
    assert a.first_offset == 0
    assert b.first_offset == 1


# list_streams returns every stream in the namespace.
async def test_list_streams_pagination_collects_all(
    client: streams.StreamClient, transport: streams.FakeTransport
):
    for i in range(5):
        await client.create_stream(f"s-{i}")
    seen = [summary.stream_id async for summary in client.list_streams(page_size=2)]
    assert seen == ["s-0", "s-1", "s-2", "s-3", "s-4"]
    list_calls = [c for c in transport.calls if c[0] == "list_streams"]
    assert len(list_calls) == 3


# delete_stream removes the stream; subsequent describe returns
# StreamNotFound.
async def test_delete_stream(client: streams.StreamClient):
    await client.create_stream("stream-1")
    await client.delete_stream("stream-1")
    with pytest.raises(streams.StreamNotFound):
        await client.describe_stream("stream-1")


# compute_payload_hash + new_publisher_id smoke tests.
def test_helpers_are_pure():
    assert streams.new_publisher_id() != streams.new_publisher_id()
    h1 = streams.compute_payload_hash([b"a", b"b"])
    h2 = streams.compute_payload_hash([b"a", b"b"])
    h3 = streams.compute_payload_hash([b"a", b"c"])
    assert h1 == h2 != h3
    assert len(h1) == 32  # sha256


# Sequence number must be strictly increasing for the same publisher,
# even with intervening publishers — interleaving doesn't reset state.
async def test_publisher_interleave_does_not_break_dedup(
    client: streams.StreamClient,
):
    stream = await client.create_stream("stream-1")
    await stream.publish([b"a"], publisher_id="pub-1", sequence=1)
    await stream.publish([b"x"], publisher_id="pub-2", sequence=1)
    await stream.publish([b"b"], publisher_id="pub-1", sequence=2)
    with pytest.raises(streams.SeqRegression):
        await stream.publish([b"c"], publisher_id="pub-1", sequence=1)


# Auto-flush on the interval: a single publish on the Publisher gets
# flushed after the flush_interval.
async def test_publisher_scheduled_flush(client: streams.StreamClient):
    stream = await client.create_stream("stream-1")
    pub = stream.publisher(publisher_id="pub-1", flush_interval_ms=5)
    await pub.publish(b"a")
    # Give the scheduled flush a chance to fire.
    await asyncio.sleep(0.05)
    desc = await stream.describe()
    assert desc.state.head_offset == 1
    await pub.flush()


async def test_publisher_reschedules_when_item_added_during_scheduled_flush():
    class BlockingTransport(streams.FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.first_publish_started = asyncio.Event()
            self.release_first_publish = asyncio.Event()
            self.publish_count = 0

        async def publish(
            self, req: _transport.PublishRequest
        ) -> streams.PublishResult:
            self.publish_count += 1
            if self.publish_count == 1:
                self.first_publish_started.set()
                await self.release_first_publish.wait()
            return await super().publish(req)

    transport = BlockingTransport()
    client = streams.StreamClient(transport, namespace="ns-1")
    stream = await client.create_stream("stream-1")
    pub = stream.publisher(publisher_id="pub-1", flush_interval_ms=1)

    await pub.publish(b"a")
    await asyncio.wait_for(transport.first_publish_started.wait(), timeout=1)
    await pub.publish(b"b")
    transport.release_first_publish.set()

    desc: streams.StreamDescription | None = None
    for _ in range(100):
        desc = await stream.describe()
        if desc.state.head_offset == 2:
            break
        await asyncio.sleep(0.01)

    assert desc is not None
    assert desc.state.head_offset == 2
    assert transport.publish_count == 2
    await pub.flush()
