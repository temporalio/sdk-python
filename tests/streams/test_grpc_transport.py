"""End-to-end tests for the native-streams gRPC transport."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import grpc
import google.protobuf.message
import pytest

import temporalio.service
from temporalio import streams
from temporalio.api.server.chasm.lib.stream.proto.v1 import (
    message_pb2 as stream_message_pb2,
)
from temporalio.api.server.chasm.lib.stream.proto.v1 import (
    request_response_pb2 as stream_pb2,
)
from temporalio.streams._grpc import STREAM_SERVICE


@dataclass
class _StreamState:
    stream_id: str
    created_by: str = ""
    created_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    head_offset: int = 0
    base_offset: int = 0
    committed_txn_id: int = 0
    closed: bool = False
    publishers: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    items: dict[int, stream_pb2.PublishItem] = field(default_factory=dict)


class _StreamService:
    def __init__(self) -> None:
        self.streams: dict[tuple[str, str], _StreamState] = {}

    async def create_stream(
        self, req: stream_pb2.CreateStreamRequest, context: grpc.aio.ServicerContext
    ) -> stream_pb2.CreateStreamResponse:
        key = (req.namespace_id, req.stream_id)
        if key in self.streams:
            await context.abort(grpc.StatusCode.ALREADY_EXISTS, "stream already exists")
        self.streams[key] = _StreamState(
            stream_id=req.stream_id, created_by=req.created_by
        )
        return stream_pb2.CreateStreamResponse(stream_id=req.stream_id)

    async def describe_stream(
        self, req: stream_pb2.DescribeStreamRequest, context: grpc.aio.ServicerContext
    ) -> stream_pb2.DescribeStreamResponse:
        st = await self._require(req.namespace_id, req.stream_id, context)
        state = stream_message_pb2.StreamState(
            head_offset=st.head_offset,
            base_offset=st.base_offset,
            committed_txn_id=st.committed_txn_id,
            closed=st.closed,
            created_by=st.created_by,
        )
        state.created_time.FromDatetime(st.created_time)
        return stream_pb2.DescribeStreamResponse(
            state=state,
            publisher_count=len(st.publishers),
        )

    async def publish(
        self, req: stream_pb2.PublishRequest, context: grpc.aio.ServicerContext
    ) -> stream_pb2.PublishResponse:
        st = await self._require(req.namespace_id, req.stream_id, context)
        if st.closed:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "stream is closed")
        if _payload_hash(req.items) != req.payload_hash:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "payload_hash does not match items",
            )
        pub = st.publishers.get(req.publisher_id)
        if pub is not None:
            last_seq, prior_first, prior_count = pub
            if req.sequence < last_seq:
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "sequence regression: seq < last_seq for this publisher",
                )
            if req.sequence == last_seq:
                return stream_pb2.PublishResponse(
                    first_offset=prior_first, item_count=prior_count
                )

        first_offset = st.head_offset
        for i, item in enumerate(req.items):
            st.items[first_offset + i] = stream_pb2.PublishItem(
                data=item.data, topic=item.topic
            )
        item_count = len(req.items)
        st.head_offset += item_count
        st.committed_txn_id += 1
        st.publishers[req.publisher_id] = (req.sequence, first_offset, item_count)
        return stream_pb2.PublishResponse(
            first_offset=first_offset, item_count=item_count
        )

    async def read_range(
        self, req: stream_pb2.ReadRangeRequest, context: grpc.aio.ServicerContext
    ) -> stream_pb2.ReadRangeResponse:
        st = await self._require(req.namespace_id, req.stream_id, context)
        if req.start_offset < st.base_offset:
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "requested offset is below base_offset (truncated)",
            )
        scan_end = min(req.end_offset, st.head_offset)
        topics = set(req.topics)
        items = []
        offsets = []
        for offset in range(req.start_offset, scan_end):
            item = st.items[offset]
            if topics and item.topic not in topics:
                continue
            items.append(item)
            offsets.append(offset)
        return stream_pb2.ReadRangeResponse(
            items=items, offsets=offsets, next_offset=scan_end
        )

    async def close(
        self, req: stream_pb2.CloseRequest, context: grpc.aio.ServicerContext
    ) -> stream_pb2.CloseResponse:
        st = await self._require(req.namespace_id, req.stream_id, context)
        st.closed = True
        return stream_pb2.CloseResponse()

    async def truncate(
        self, req: stream_pb2.TruncateRequest, context: grpc.aio.ServicerContext
    ) -> stream_pb2.TruncateResponse:
        st = await self._require(req.namespace_id, req.stream_id, context)
        if req.up_to_offset > st.head_offset:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "truncate: up_to_offset exceeds head_offset",
            )
        if req.up_to_offset < st.base_offset:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "truncate: up_to_offset is below base_offset",
            )
        st.base_offset = req.up_to_offset
        return stream_pb2.TruncateResponse(new_base_offset=st.base_offset)

    async def delete_stream(
        self, req: stream_pb2.DeleteStreamRequest, context: grpc.aio.ServicerContext
    ) -> stream_pb2.DeleteStreamResponse:
        await self._require(req.namespace_id, req.stream_id, context)
        del self.streams[(req.namespace_id, req.stream_id)]
        return stream_pb2.DeleteStreamResponse()

    async def list_streams(
        self, req: stream_pb2.ListStreamsRequest, context: grpc.aio.ServicerContext
    ) -> stream_pb2.ListStreamsResponse:
        del context
        summaries = []
        for (namespace, _), st in self.streams.items():
            if namespace != req.namespace_id:
                continue
            summary = stream_pb2.StreamSummary(
                stream_id=st.stream_id,
                head_offset=st.head_offset,
                base_offset=st.base_offset,
                closed=st.closed,
            )
            summary.created_time.FromDatetime(st.created_time)
            summaries.append(summary)
        summaries.sort(key=lambda item: item.stream_id)
        start = int(req.next_page_token.decode("ascii")) if req.next_page_token else 0
        if req.page_size <= 0:
            return stream_pb2.ListStreamsResponse(streams=summaries[start:])
        end = start + req.page_size
        token = str(end).encode("ascii") if end < len(summaries) else b""
        return stream_pb2.ListStreamsResponse(
            streams=summaries[start:end],
            next_page_token=token,
        )

    async def _require(
        self, namespace_id: str, stream_id: str, context: grpc.aio.ServicerContext
    ) -> _StreamState:
        stream = self.streams.get((namespace_id, stream_id))
        if stream is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "stream not found")
        assert stream is not None
        return stream


@pytest.fixture
async def grpc_stream_target() -> AsyncIterator[str]:
    service = _StreamService()
    server = grpc.aio.server()
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                STREAM_SERVICE,
                {
                    "CreateStream": _handler(
                        service.create_stream,
                        stream_pb2.CreateStreamRequest,
                        stream_pb2.CreateStreamResponse,
                    ),
                    "DescribeStream": _handler(
                        service.describe_stream,
                        stream_pb2.DescribeStreamRequest,
                        stream_pb2.DescribeStreamResponse,
                    ),
                    "Publish": _handler(
                        service.publish,
                        stream_pb2.PublishRequest,
                        stream_pb2.PublishResponse,
                    ),
                    "ReadRange": _handler(
                        service.read_range,
                        stream_pb2.ReadRangeRequest,
                        stream_pb2.ReadRangeResponse,
                    ),
                    "Close": _handler(
                        service.close,
                        stream_pb2.CloseRequest,
                        stream_pb2.CloseResponse,
                    ),
                    "Truncate": _handler(
                        service.truncate,
                        stream_pb2.TruncateRequest,
                        stream_pb2.TruncateResponse,
                    ),
                    "DeleteStream": _handler(
                        service.delete_stream,
                        stream_pb2.DeleteStreamRequest,
                        stream_pb2.DeleteStreamResponse,
                    ),
                    "ListStreams": _handler(
                        service.list_streams,
                        stream_pb2.ListStreamsRequest,
                        stream_pb2.ListStreamsResponse,
                    ),
                },
            ),
        )
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        await server.stop(grace=0)


async def test_grpc_transport_create_publish_read_describe(
    grpc_stream_target: str,
):
    transport = streams.GrpcTransport.connect(grpc_stream_target)
    client = streams.StreamClient(transport, namespace="ns-1")
    stream = await client.create_stream("stream-1", created_by="tester")

    result = await stream.publish(
        [
            streams.PublishItem(data=b"hello", topic="alpha"),
            streams.PublishItem(data=b"world", topic="beta"),
        ],
        publisher_id="pub-1",
        sequence=1,
    )
    assert result.first_offset == 0
    assert result.item_count == 2

    got: list[tuple[int, str, bytes]] = []
    async for offset, item in stream.read_range(start_offset=0, end_offset=2):
        got.append((offset, item.topic, item.data))
    assert got == [(0, "alpha", b"hello"), (1, "beta", b"world")]

    desc = await stream.describe()
    assert desc.state.stream_id == "stream-1"
    assert desc.state.head_offset == 2
    assert desc.publisher_count == 1
    await transport.close()


async def test_grpc_transport_topic_filter_preserves_offsets(grpc_stream_target: str):
    transport = streams.GrpcTransport.connect(grpc_stream_target)
    client = streams.StreamClient(transport, namespace="ns-1")
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
        start_offset=0, end_offset=3, topics=["beta"]
    ):
        got.append((offset, item.data))
    assert got == [(1, b"b")]
    await transport.close()


async def test_grpc_transport_maps_typed_errors(grpc_stream_target: str):
    transport = streams.GrpcTransport.connect(grpc_stream_target)
    client = streams.StreamClient(transport, namespace="ns-1")
    stream = await client.create_stream("stream-1")
    await stream.publish([b"first"], publisher_id="pub-1", sequence=5)

    with pytest.raises(streams.SeqRegression):
        await stream.publish([b"regression"], publisher_id="pub-1", sequence=3)

    with pytest.raises(streams.TruncateBeyondHead):
        await stream.truncate(100)

    await stream.truncate(1)
    with pytest.raises(streams.OffsetTruncated):
        async for _ in stream.read_range(start_offset=0, end_offset=1):
            pass
    await transport.close()


async def test_grpc_transport_from_service_client_lists_streams(
    grpc_stream_target: str,
):
    service_client = await temporalio.service.ServiceClient.connect(
        temporalio.service.ConnectConfig(target_host=grpc_stream_target, lazy=True)
    )
    client = streams.StreamClient.from_service_client(service_client, namespace="ns-1")

    for i in range(3):
        await client.create_stream(f"stream-{i}")
    listed = [item.stream_id async for item in client.list_streams(page_size=2)]
    assert listed == ["stream-0", "stream-1", "stream-2"]

    transport = client.transport
    assert isinstance(transport, streams.GrpcTransport)
    await transport.close()


def _handler(
    func: Any,
    request_type: type[google.protobuf.message.Message],
    response_type: type[google.protobuf.message.Message],
) -> grpc.RpcMethodHandler:
    del response_type
    return grpc.unary_unary_rpc_method_handler(
        func,
        request_deserializer=request_type.FromString,
        response_serializer=_serialize_proto,
    )


def _serialize_proto(message: google.protobuf.message.Message) -> bytes:
    return message.SerializeToString()


def _payload_hash(items: Iterable[stream_pb2.PublishItem]) -> bytes:
    h = hashlib.sha256()
    for item in items:
        h.update(item.data)
    return h.digest()
