"""The Redis Stream Store.

A client takes it in ``stream_config``, and its Workers use the same Store::

    from temporalio.client import Client, StreamConfig
    from temporalio.contrib.redis.streams import RedisStreamStore

    client = await Client.connect(
        "localhost:7233",
        stream_config=StreamConfig(stores=[RedisStreamStore("redis://localhost:6379")]),
    )

.. warning::
    This module is experimental and subject to change.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

import temporalio.bridge.runtime
import temporalio.bridge.streams
import temporalio.client._stream_store
import temporalio.runtime


class RedisStreamStore(temporalio.client._stream_store._BridgeStreamStore):
    """A Stream Store that keeps its Streams in Redis 7.0 or later.

    It runs the Stream runtime of the bridge on the Redis Store of the
    ``temporalio-streams-redis`` crate. It connects at its first call, so building
    it needs no server, and a server that is down fails that call. A failed call
    raises :py:class:`temporalio.exceptions.StreamError`.

    A key has no namespace, so a Stream id is unique in one Redis and key prefix.
    Give each namespace its own key prefix to keep their Streams apart.

    .. warning::
        This API is experimental and subject to change.
    """

    def __init__(
        self,
        url: str | Sequence[str],
        *,
        cluster: bool = False,
        key_prefix: str | None = None,
        retention: timedelta | None = None,
        last_write_ttl: timedelta | None = None,
        response_timeout: timedelta | None = None,
        wait_replicas: int = 0,
        wait_timeout: timedelta | None = None,
        name: str = "redis",
        runtime: temporalio.runtime.Runtime | None = None,
    ) -> None:
        """Create a Redis Store. It builds its Stream runtime and connects at its first call.

        A bad option raises ``ValueError`` at that first call, with the message of the
        Redis crate.

        Args:
            url: A ``redis://`` or ``rediss://`` URL, or the seed URLs of a cluster.
                Credentials and TLS come from the URL.
            cluster: Whether the URLs are of a Redis Cluster.
            key_prefix: The prefix of every key. ``None`` takes ``temporal-streams``.
                A Stream id is unique in one Redis and key prefix.
            retention: The default Retention of a new Stream, which is how long the
                Stream stays readable after its close. ``None`` takes 7 days.
            last_write_ttl: How long an open Stream stays after its last write.
                ``None`` keeps an open Stream and every Record until its close.
            response_timeout: The longest time that one Redis call can take. ``None``
                takes 5 seconds.
            wait_replicas: How many replicas must have each write before it answers,
                with the Redis ``WAIT`` command. Zero sends no ``WAIT``.
            wait_timeout: How long a write waits for its replicas, at most half of
                ``response_timeout``. ``None`` takes 1 second.
            name: Name of the Store, which the refs of its Streams carry, so that every
                process can route a ref that it gets.
            runtime: Runtime whose Tokio threads run the Store. ``None`` takes
                :py:meth:`temporalio.runtime.Runtime.default` at the first call.
        """
        temporalio.client._stream_store._check_not_negative(
            retention=retention,
            last_write_ttl=last_write_ttl,
            response_timeout=response_timeout,
            wait_timeout=wait_timeout,
        )
        super().__init__(name=name, runtime=runtime)
        self._urls = [url] if isinstance(url, str) else list(url)
        self._cluster = cluster
        self._key_prefix = key_prefix
        self._retention = retention
        self._last_write_ttl = last_write_ttl
        self._response_timeout = response_timeout
        self._wait_replicas = wait_replicas
        self._wait_timeout = wait_timeout

    def _new_stream_runtime(
        self, runtime: temporalio.bridge.runtime.Runtime
    ) -> temporalio.bridge.streams.StreamRuntime:
        return temporalio.bridge.streams.StreamRuntime.new_redis(
            runtime,
            urls=self._urls,
            cluster=self._cluster,
            key_prefix=self._key_prefix,
            retention=self._retention,
            last_write_ttl=self._last_write_ttl,
            response_timeout=self._response_timeout,
            name=self._name,
            wait_replicas=self._wait_replicas,
            wait_timeout=self._wait_timeout,
        )


__all__ = ["RedisStreamStore"]
