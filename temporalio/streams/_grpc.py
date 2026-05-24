"""gRPC transport for native streams."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, TypeVar, cast
from urllib.parse import urlparse

import google.protobuf.message
import google.protobuf.timestamp_pb2

import temporalio.service
from temporalio.api.server.chasm.lib.stream.proto.v1 import (
    message_pb2 as stream_message_pb2,
)
from temporalio.api.server.chasm.lib.stream.proto.v1 import (
    request_response_pb2 as stream_pb2,
)

from ._transport import (
    CloseRequest,
    CreateStreamRequest,
    DeleteStreamRequest,
    DescribeStreamRequest,
    ListStreamsRequest,
    OffsetTruncated,
    PublishAbortedByClose,
    PublishAbortedByOwnerChange,
    PublishGroupAborted,
    PublishRequest,
    ReadRangeRequest,
    ReadRangeResponse,
    SeqRegression,
    StreamAlreadyExists,
    StreamClosed,
    StreamNotFound,
    StreamServiceError,
    Transport,
    TruncateBeyondBase,
    TruncateBeyondHead,
    TruncateBlockedBySubscriber,
    TruncateRequest,
)
from ._types import (
    ListStreamsPage,
    PublishItem,
    PublishResult,
    StreamCloseReason,
    StreamDescription,
    StreamState,
    StreamSummary,
    TruncateResult,
)

STREAM_SERVICE = "temporal.server.chasm.lib.stream.proto.v1.StreamService"

ResponseT = TypeVar("ResponseT", bound=google.protobuf.message.Message)

if TYPE_CHECKING:
    import grpc
    import grpc.aio


class GrpcTransport(Transport):
    """Transport that calls the server StreamService over gRPC.

    The transport uses gRPC's generic unary-unary API, so it only needs
    the message bindings rather than generated StreamService client
    stubs.
    """

    def __init__(
        self,
        channel: grpc.aio.Channel,
        *,
        rpc_metadata: Mapping[str, str | bytes] | None = None,
        rpc_timeout: timedelta | None = None,
        service: str = STREAM_SERVICE,
    ) -> None:
        self._channel = channel
        self._rpc_metadata = dict(rpc_metadata or {})
        self._rpc_timeout = rpc_timeout
        self._service = service

    @classmethod
    def connect(
        cls,
        target_host: str,
        *,
        tls: bool | temporalio.service.TLSConfig | None = None,
        api_key: str | None = None,
        rpc_metadata: Mapping[str, str | bytes] | None = None,
        rpc_timeout: timedelta | None = None,
        service: str = STREAM_SERVICE,
    ) -> GrpcTransport:
        """Create a transport with its own gRPC channel."""
        grpc = _grpc_module()
        target = _normalize_target_host(target_host)
        metadata = dict(rpc_metadata or {})
        if api_key is not None and "authorization" not in metadata:
            metadata["authorization"] = f"Bearer {api_key}"

        if isinstance(tls, temporalio.service.TLSConfig):
            credentials = grpc.ssl_channel_credentials(
                root_certificates=tls.server_root_ca_cert,
                private_key=tls.client_private_key,
                certificate_chain=tls.client_cert,
            )
            options: list[tuple[str, str]] = []
            if tls.domain:
                options.append(("grpc.ssl_target_name_override", tls.domain))
            channel = grpc.aio.secure_channel(target, credentials, options=options)
        elif tls:
            channel = grpc.aio.secure_channel(target, grpc.ssl_channel_credentials())
        else:
            channel = grpc.aio.insecure_channel(target)

        return cls(
            channel,
            rpc_metadata=metadata,
            rpc_timeout=rpc_timeout,
            service=service,
        )

    @classmethod
    def from_service_client(
        cls,
        service_client: temporalio.service.ServiceClient,
        *,
        rpc_metadata: Mapping[str, str | bytes] | None = None,
        rpc_timeout: timedelta | None = None,
        service: str = STREAM_SERVICE,
    ) -> GrpcTransport:
        """Create a direct gRPC transport from a ServiceClient config."""
        metadata = dict(service_client.config.rpc_metadata)
        metadata.update(rpc_metadata or {})
        tls = service_client.config.tls
        if tls is None and service_client.config.api_key is not None:
            tls = True
        return cls.connect(
            service_client.config.target_host,
            tls=tls,
            api_key=service_client.config.api_key,
            rpc_metadata=metadata,
            rpc_timeout=rpc_timeout,
            service=service,
        )

    async def close(self) -> None:
        """Close this transport's gRPC channel."""
        await self._channel.close()

    async def create_stream(self, req: CreateStreamRequest) -> str:
        pb_req = stream_pb2.CreateStreamRequest(
            namespace_id=req.namespace_id,
            stream_id=req.stream_id,
            retention_max_bytes=req.options.retention_max_bytes,
            retention_max_items=req.options.retention_max_items,
            segment_max_items=req.options.segment_max_items,
            segment_max_bytes=req.options.segment_max_bytes,
            owner_workflow_id=req.options.owner_workflow_id,
            owner_run_id=req.options.owner_run_id,
            created_by=req.options.created_by,
        )
        if req.options.publisher_ttl is not None:
            pb_req.publisher_ttl.FromTimedelta(req.options.publisher_ttl)
        resp = await self._call("CreateStream", pb_req, stream_pb2.CreateStreamResponse)
        return resp.stream_id

    async def describe_stream(self, req: DescribeStreamRequest) -> StreamDescription:
        resp = await self._call(
            "DescribeStream",
            stream_pb2.DescribeStreamRequest(
                namespace_id=req.namespace_id,
                stream_id=req.stream_id,
            ),
            stream_pb2.DescribeStreamResponse,
        )
        state = _stream_state_from_proto(resp.state, stream_id=req.stream_id)
        return StreamDescription(
            state=state,
            inflight_count=resp.inflight_count,
            subscription_count=resp.subscription_count,
            publisher_count=resp.publisher_count,
            stuck_subscription_count=resp.stuck_subscription_count,
            force_truncated_subscription_count=(
                resp.force_truncated_subscription_count
            ),
        )

    async def publish(self, req: PublishRequest) -> PublishResult:
        resp = await self._call(
            "Publish",
            stream_pb2.PublishRequest(
                namespace_id=req.namespace_id,
                stream_id=req.stream_id,
                publisher_id=req.publisher_id,
                sequence=req.sequence,
                items=[_publish_item_to_proto(item) for item in req.items],
                payload_hash=req.payload_hash,
            ),
            stream_pb2.PublishResponse,
        )
        return PublishResult(first_offset=resp.first_offset, item_count=resp.item_count)

    async def read_range(self, req: ReadRangeRequest) -> ReadRangeResponse:
        resp = await self._call(
            "ReadRange",
            stream_pb2.ReadRangeRequest(
                namespace_id=req.namespace_id,
                stream_id=req.stream_id,
                start_offset=req.start_offset,
                end_offset=req.end_offset,
                topics=req.topics,
            ),
            stream_pb2.ReadRangeResponse,
        )
        return ReadRangeResponse(
            items=[_publish_item_from_proto(item) for item in resp.items],
            next_offset=resp.next_offset,
        )

    async def close_stream(self, req: CloseRequest) -> None:
        await self._call(
            "Close",
            stream_pb2.CloseRequest(
                namespace_id=req.namespace_id,
                stream_id=req.stream_id,
                closed_by=req.closed_by,
                close_reason=cast(Any, req.close_reason.value),
            ),
            stream_pb2.CloseResponse,
        )

    async def truncate(self, req: TruncateRequest) -> TruncateResult:
        resp = await self._call(
            "Truncate",
            stream_pb2.TruncateRequest(
                namespace_id=req.namespace_id,
                stream_id=req.stream_id,
                up_to_offset=req.up_to_offset,
                force=req.force,
            ),
            stream_pb2.TruncateResponse,
        )
        return TruncateResult(
            new_base_offset=resp.new_base_offset,
            force_truncated_subscription_count=(
                resp.force_truncated_subscription_count
            ),
        )

    async def delete_stream(self, req: DeleteStreamRequest) -> None:
        await self._call(
            "DeleteStream",
            stream_pb2.DeleteStreamRequest(
                namespace_id=req.namespace_id,
                stream_id=req.stream_id,
            ),
            stream_pb2.DeleteStreamResponse,
        )

    async def list_streams(self, req: ListStreamsRequest) -> ListStreamsPage:
        resp = await self._call(
            "ListStreams",
            stream_pb2.ListStreamsRequest(
                namespace_id=req.namespace_id,
                next_page_token=req.next_page_token or b"",
                page_size=req.page_size,
            ),
            stream_pb2.ListStreamsResponse,
        )
        return ListStreamsPage(
            streams=[_stream_summary_from_proto(item) for item in resp.streams],
            next_page_token=resp.next_page_token or None,
        )

    async def _call(
        self,
        rpc: str,
        req: google.protobuf.message.Message,
        resp_type: type[ResponseT],
    ) -> ResponseT:
        grpc = _grpc_module()
        try:
            call = self._channel.unary_unary(
                f"/{self._service}/{rpc}",
                request_serializer=_serialize_proto,
                response_deserializer=resp_type.FromString,
            )
            return await call(
                req,
                timeout=(
                    self._rpc_timeout.total_seconds() if self._rpc_timeout else None
                ),
                metadata=tuple(self._rpc_metadata.items()),
            )
        except grpc.aio.AioRpcError as err:
            raise _map_rpc_error(err) from err


def _publish_item_to_proto(item: PublishItem) -> stream_pb2.PublishItem:
    return stream_pb2.PublishItem(data=item.data, topic=item.topic)


def _publish_item_from_proto(item: stream_pb2.PublishItem) -> PublishItem:
    return PublishItem(data=item.data, topic=item.topic)


def _serialize_proto(message: google.protobuf.message.Message) -> bytes:
    return message.SerializeToString()


def _stream_state_from_proto(
    state: stream_message_pb2.StreamState, *, stream_id: str
) -> StreamState:
    return StreamState(
        stream_id=stream_id,
        head_offset=state.head_offset,
        base_offset=state.base_offset,
        committed_txn_id=state.committed_txn_id,
        closed=state.closed,
        created_by=state.created_by,
        created_time=_optional_timestamp_to_datetime(state, "created_time"),
        closed_by=state.closed_by,
        closed_time=_optional_timestamp_to_datetime(state, "closed_time"),
        close_reason=_close_reason_from_value(state.close_reason),
        owner_workflow_id=state.owner_workflow_id,
        owner_run_id=state.owner_run_id,
        retention_max_bytes=state.retention_max_bytes,
        retention_max_items=state.retention_max_items,
        segment_max_items=state.segment_max_items,
        segment_max_bytes=state.segment_max_bytes,
    )


def _stream_summary_from_proto(summary: stream_pb2.StreamSummary) -> StreamSummary:
    return StreamSummary(
        stream_id=summary.stream_id,
        head_offset=summary.head_offset,
        base_offset=summary.base_offset,
        closed=summary.closed,
        created_time=_optional_timestamp_to_datetime(summary, "created_time"),
        closed_time=_optional_timestamp_to_datetime(summary, "closed_time"),
    )


def _optional_timestamp_to_datetime(
    msg: google.protobuf.message.Message,
    field_name: str,
) -> datetime | None:
    if not msg.HasField(field_name):
        return None
    ts = getattr(msg, field_name)
    if not isinstance(ts, google.protobuf.timestamp_pb2.Timestamp):
        return None
    return ts.ToDatetime().replace(tzinfo=timezone.utc)


def _close_reason_from_value(value: int) -> StreamCloseReason:
    try:
        return StreamCloseReason(value)
    except ValueError:
        return StreamCloseReason.UNSPECIFIED


def _grpc_module() -> Any:
    try:
        import grpc
    except ImportError as err:
        raise RuntimeError(
            "GrpcTransport requires grpcio. Install temporalio with the grpc extra."
        ) from err
    return grpc


def _normalize_target_host(target_host: str) -> str:
    if "://" not in target_host:
        return target_host
    parsed = urlparse(target_host)
    return parsed.netloc or parsed.path


def _map_rpc_error(err: Any) -> StreamServiceError:
    message = err.details() or str(err)
    lower_message = message.lower()
    status = err.code()
    grpc = _grpc_module()
    if status == grpc.StatusCode.NOT_FOUND:
        return StreamNotFound(message)
    if status == grpc.StatusCode.ALREADY_EXISTS:
        return StreamAlreadyExists(message)
    if status == grpc.StatusCode.UNAVAILABLE:
        return PublishAbortedByOwnerChange(message)
    if "sequence regression" in lower_message:
        return SeqRegression(message)
    if "stream is closed" in lower_message:
        return StreamClosed(message)
    if "publish aborted because stream was closed" in lower_message:
        return PublishAbortedByClose(message)
    if "group" in lower_message and "abort" in lower_message:
        return PublishGroupAborted(message)
    if "up_to_offset exceeds head_offset" in lower_message:
        return TruncateBeyondHead(message)
    if "requested offset is below base_offset" in lower_message:
        return OffsetTruncated(message)
    if "below base_offset" in lower_message and "truncate:" in lower_message:
        return TruncateBeyondBase(message)
    if "truncate blocked" in lower_message:
        return TruncateBlockedBySubscriber(message)
    return StreamServiceError(message)
