"""Native streams smoke test against a running Temporal source server."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import timedelta
from typing import Any

from temporalio import streams


async def run(args: argparse.Namespace) -> dict[str, Any]:
    transport = streams.GrpcTransport.connect(
        args.target_host,
        rpc_timeout=timedelta(seconds=args.rpc_timeout_seconds),
    )
    client = streams.StreamClient(transport, namespace=args.namespace)
    stream_id = args.stream_id or f"m6-e2e-{uuid.uuid4().hex[:8]}"

    try:
        stream = await client.create_stream(
            stream_id,
            created_by="native-streams-python-e2e",
            segment_max_items=10,
        )
        first_batch = [
            streams.PublishItem(data=b"alpha-0", topic="alpha"),
            streams.PublishItem(data=b"beta-1", topic="beta"),
        ]
        second_batch = [
            streams.PublishItem(data=b"alpha-2", topic="alpha"),
        ]

        first = await stream.publish(
            first_batch,
            publisher_id="native-streams-e2e",
            sequence=1,
        )
        replay = await stream.publish(
            first_batch,
            publisher_id="native-streams-e2e",
            sequence=1,
        )
        second = await stream.publish(
            second_batch,
            publisher_id="native-streams-e2e",
            sequence=2,
        )

        all_items = [
            (offset, item.topic, item.data.decode())
            async for offset, item in stream.read_range(start_offset=0, end_offset=3)
        ]
        beta_items = [
            (offset, item.topic, item.data.decode())
            async for offset, item in stream.read_range(
                start_offset=0,
                end_offset=3,
                topics=["beta"],
            )
        ]
        description = await stream.describe()
        listed = [
            summary.stream_id
            async for summary in client.list_streams(page_size=100)
            if summary.stream_id == stream_id
        ]

        result = {
            "stream_id": stream_id,
            "publish": [first.first_offset, first.item_count],
            "dedup_replay": [replay.first_offset, replay.item_count],
            "second_publish": [second.first_offset, second.item_count],
            "read": all_items,
            "topic_read": beta_items,
            "head": description.state.head_offset,
            "listed": listed,
        }

        assert (first.first_offset, first.item_count) == (0, 2), result
        assert (replay.first_offset, replay.item_count) == (0, 2), result
        assert (second.first_offset, second.item_count) == (2, 1), result
        assert all_items == [
            (0, "alpha", "alpha-0"),
            (1, "beta", "beta-1"),
            (2, "alpha", "alpha-2"),
        ], result
        assert beta_items == [(1, "beta", "beta-1")], result
        assert description.state.head_offset == 3, result
        assert listed == [stream_id], result

        await client.delete_stream(stream_id)
        return result
    finally:
        await transport.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-host", default="127.0.0.1:7233")
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--stream-id", default="")
    parser.add_argument("--rpc-timeout-seconds", type=float, default=10.0)
    return parser.parse_args()


def main() -> None:
    result = asyncio.run(run(parse_args()))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
