from __future__ import annotations

import asyncio
import logging
import threading
from datetime import timedelta
from typing import Any

import pytest

from temporalio.contrib.aws.secrets_manager._cache import (
    FAILED_REFRESH_RETRY_INTERVAL_SECONDS,
    CacheEntry,
    SecretCache,
    SecretFetchError,
    SecretIDs,
)
from temporalio.contrib.aws.secrets_manager._telemetry import (
    METRIC_CACHE_LOOKUP,
    METRIC_FETCH_FAILED,
    METRIC_FETCH_LATENCY,
    FetchPath,
    LambdaExtensionStatusError,
)
from tests.contrib.aws.secrets_manager.helpers import (
    FakeFetcher,
    MetricsRecorder,
    new_cert_key,
    wait_until,
)

LOGGER = "temporalio.contrib.aws.secrets_manager"


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Captured records from the extension's logger only."""
    return [r for r in caplog.records if r.name == LOGGER]


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _new_cache(
    fetcher: FakeFetcher,
    ids: SecretIDs,
    refresh_interval: timedelta = timedelta(minutes=5),
    max_staleness: timedelta = timedelta(hours=1),
) -> tuple[SecretCache, _Clock]:
    cache = SecretCache(fetcher, ids, refresh_interval, max_staleness)
    clock = _Clock()
    cache.now = clock
    return cache, clock


async def test_hit_miss_and_rotation(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO, logger=LOGGER)
    fetcher = FakeFetcher()
    fetcher.set("key-id", "key-1", "v1")
    cache, clock = _new_cache(fetcher, SecretIDs(api_key="key-id"))
    assert cache.api_key
    metrics = MetricsRecorder()

    assert await cache.get(cache.api_key, metrics.telemetry) == "key-1"
    clock.t += 60
    assert await cache.get(cache.api_key, metrics.telemetry) == "key-1"
    assert fetcher.calls["key-id"] == 1

    updates = metrics.updates()
    base = {"secret_type": "api_key", "fetch_path": "sdk"}
    assert [a for n, _, a in updates if n == METRIC_CACHE_LOOKUP] == [
        {**base, "result": "miss"},
        {**base, "result": "hit"},
    ]
    assert [a for n, _, a in updates if n == METRIC_FETCH_LATENCY] == [
        {**base, "outcome": "success"}
    ]
    assert not [u for u in updates if u[0] == METRIC_FETCH_FAILED]
    assert [r.getMessage() for r in _records(caplog)] == [
        "Fetched AWS Secrets Manager secret (secret_id=key-id, secret_type=api_key, fetch_path=sdk, version_id=v1)"
    ]

    # Same version after the refresh interval doesn't log
    caplog.clear()
    clock.t += 300
    assert await cache.get(cache.api_key, metrics.telemetry) == "key-1"
    assert fetcher.calls["key-id"] == 2
    assert not _records(caplog)

    # Rotated version logs
    fetcher.set("key-id", "key-2", "v2")
    clock.t += 300
    assert await cache.get(cache.api_key, metrics.telemetry) == "key-2"
    assert [r.getMessage() for r in _records(caplog)] == [
        "Fetched updated AWS Secrets Manager secret (secret_id=key-id, secret_type=api_key, previous_version_id=v1, version_id=v2)"
    ]


async def test_zero_refresh_interval_always_fetches():
    fetcher = FakeFetcher()
    fetcher.set("key-id", "key", "v1")
    cache, _ = _new_cache(fetcher, SecretIDs(api_key="key-id"), timedelta(0))
    assert cache.api_key
    tel = MetricsRecorder().telemetry
    await cache.get(cache.api_key, tel)
    await cache.get(cache.api_key, tel)
    assert fetcher.calls["key-id"] == 2


async def test_failure_falls_back_to_stale(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO, logger=LOGGER)
    fetcher = FakeFetcher(FetchPath.EXTENSION)
    fetcher.set("key-id", "key-1", "v1")
    cache, clock = _new_cache(fetcher, SecretIDs(api_key="key-id"))
    assert cache.api_key
    metrics = MetricsRecorder()
    await cache.get(cache.api_key, metrics.telemetry)
    metrics.updates()
    caplog.clear()

    # Refresh fails, falling back
    fetcher.fail("key-id", LambdaExtensionStatusError(503, "unavailable"))
    clock.t += 300
    assert await cache.get(cache.api_key, metrics.telemetry) == "key-1"
    assert fetcher.calls["key-id"] == 2
    base = {"secret_type": "api_key", "fetch_path": "extension"}
    updates = metrics.updates()
    assert [a for n, _, a in updates if n == METRIC_FETCH_FAILED] == [
        {**base, "failure_reason": "server_error", "fallback": "stale"}
    ]
    assert [a for n, _, a in updates if n == METRIC_CACHE_LOOKUP] == [
        {**base, "result": "miss_with_fallback"}
    ]
    assert [a for n, _, a in updates if n == METRIC_FETCH_LATENCY] == [
        {**base, "outcome": "failure"}
    ]
    records = _records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert (
        records[0]
        .getMessage()
        .startswith(
            "Failed refreshing AWS Secrets Manager secret, using previously fetched value (secret_id=key-id, secret_type=api_key, error="
        )
    )
    assert "age=0:05:00, max_staleness=1:00:00" in records[0].getMessage()

    # No retry until the retry interval passes
    clock.t += FAILED_REFRESH_RETRY_INTERVAL_SECONDS - 1
    assert await cache.get(cache.api_key, metrics.telemetry) == "key-1"
    assert fetcher.calls["key-id"] == 2
    assert [a for n, _, a in metrics.updates() if n == METRIC_CACHE_LOOKUP] == [
        {**base, "result": "miss_with_fallback"}
    ]

    # Recovery with the same version still logs
    caplog.clear()
    fetcher.set("key-id", "key-1", "v1")
    clock.t += 1
    assert await cache.get(cache.api_key, metrics.telemetry) == "key-1"
    assert fetcher.calls["key-id"] == 3
    assert [r.getMessage() for r in _records(caplog)] == [
        "Fetched updated AWS Secrets Manager secret (secret_id=key-id, secret_type=api_key, previous_version_id=v1, version_id=v1)"
    ]


async def test_failure_without_fallback():
    fetcher = FakeFetcher()
    err = Exception("boom")
    setattr(err, "response", {"Error": {"Code": "AccessDeniedException"}})
    fetcher.fail("key-id", err)
    cache, _ = _new_cache(fetcher, SecretIDs(api_key="key-id"))
    assert cache.api_key
    metrics = MetricsRecorder()
    with pytest.raises(SecretFetchError) as exc:
        await cache.get(cache.api_key, metrics.telemetry)
    assert str(exc.value) == 'failed fetching AWS Secrets Manager secret "key-id": boom'
    assert exc.value.__cause__ is err
    updates = metrics.updates()
    assert [a for n, _, a in updates if n == METRIC_FETCH_FAILED] == [
        {
            "secret_type": "api_key",
            "fetch_path": "sdk",
            "failure_reason": "access_denied",
            "fallback": "none",
        }
    ]
    assert not [u for u in updates if u[0] == METRIC_CACHE_LOOKUP]


async def test_max_staleness():
    fetcher = FakeFetcher()
    fetcher.set("key-id", "key-1", "v1")
    cache, clock = _new_cache(
        fetcher, SecretIDs(api_key="key-id"), max_staleness=timedelta(minutes=10)
    )
    assert cache.api_key
    tel = MetricsRecorder().telemetry
    await cache.get(cache.api_key, tel)
    fetcher.fail("key-id", Exception("boom"))
    clock.t += 9 * 60
    assert await cache.get(cache.api_key, tel) == "key-1"
    clock.t += 60
    with pytest.raises(SecretFetchError):
        await cache.get(cache.api_key, tel)


async def test_concurrent_gets_share_fetch():
    fetcher = FakeFetcher()
    fetcher.set("key-id", "key-1", "v1")
    fetcher.gate = threading.Event()
    cache, _ = _new_cache(fetcher, SecretIDs(api_key="key-id"))
    entry = cache.api_key
    assert entry
    metrics = MetricsRecorder()
    tasks = [asyncio.create_task(cache.get(entry, metrics.telemetry)) for _ in range(5)]
    # The tasks all run on the loop's next turn, so they've all joined the fetch
    # once it's started
    await wait_until(lambda: entry.in_flight is not None)
    fetcher.gate.set()
    assert await asyncio.gather(*tasks) == ["key-1"] * 5
    assert fetcher.calls["key-id"] == 1
    lookups = [a["result"] for n, _, a in metrics.updates() if n == METRIC_CACHE_LOOKUP]
    assert lookups == ["miss"] * 5


async def test_refreshing_serves_stale_to_other_callers():
    fetcher = FakeFetcher()
    fetcher.set("key-id", "key-1", "v1")
    cache, clock = _new_cache(fetcher, SecretIDs(api_key="key-id"))
    assert cache.api_key
    tel = MetricsRecorder().telemetry
    await cache.get(cache.api_key, tel)

    fetcher.set("key-id", "key-2", "v2")
    fetcher.gate = threading.Event()
    clock.t += 300
    entry = cache.api_key
    refreshing = asyncio.create_task(cache.get(entry, tel))
    await wait_until(lambda: entry.in_flight is not None)
    # While a refresh is in progress, the current value is returned immediately
    assert await cache.get(cache.api_key, tel) == "key-1"
    fetcher.gate.set()
    assert await refreshing == "key-2"
    assert fetcher.calls["key-id"] == 2


async def test_cancelled_caller_does_not_cancel_fetch():
    fetcher = FakeFetcher()
    fetcher.set("key-id", "key-1", "v1")
    fetcher.gate = threading.Event()
    cache, _ = _new_cache(fetcher, SecretIDs(api_key="key-id"))
    assert cache.api_key
    tel = MetricsRecorder().telemetry
    entry = cache.api_key
    task = asyncio.create_task(cache.get(entry, tel))
    await wait_until(lambda: entry.in_flight is not None)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    fetcher.gate.set()
    assert await cache.get(cache.api_key, tel) == "key-1"
    assert fetcher.calls["key-id"] == 1


async def test_failed_thread_start_does_not_wedge_cache(
    monkeypatch: pytest.MonkeyPatch,
):
    fetcher = FakeFetcher()
    fetcher.set("key-id", "key-1", "v1")
    cache, clock = _new_cache(fetcher, SecretIDs(api_key="key-id"))
    entry = cache.api_key
    assert entry
    tel = MetricsRecorder().telemetry
    start = threading.Thread.start
    waiter_result: list[BaseException | str] = []
    waiter_locked = threading.Event()

    def now() -> float:
        # Called with the entry lock held, so once the waiter is here, it reads
        # in_flight before the failed start can clear it
        if threading.current_thread() is waiter:
            waiter_locked.set()
        return clock()

    def wait_in_other_thread() -> None:
        try:
            waiter_result.append(asyncio.run(cache.get(entry, tel)))
        except BaseException as err:
            waiter_result.append(err)

    waiter = threading.Thread(target=wait_in_other_thread)

    def fail_start(_self: threading.Thread) -> None:
        # Another caller joins the fetch before starting it fails
        start(waiter)
        assert waiter_locked.wait(5)
        raise RuntimeError("can't start new thread")

    cache.now = now
    with monkeypatch.context() as m:
        m.setattr(threading.Thread, "start", fail_start)
        with pytest.raises(RuntimeError, match="can't start new thread"):
            await cache.get(entry, tel)
    assert entry.in_flight is None
    waiter.join(5)
    assert len(waiter_result) == 1
    assert isinstance(waiter_result[0], RuntimeError)

    # The next call starts a fetch instead of waiting on one that never started
    assert await cache.get(entry, tel) == "key-1"
    assert fetcher.calls["key-id"] == 1


async def test_failed_thread_start_falls_back_to_stale(
    monkeypatch: pytest.MonkeyPatch,
):
    fetcher = FakeFetcher()
    fetcher.set("key-id", "key-1", "v1")
    cache, clock = _new_cache(fetcher, SecretIDs(api_key="key-id"))
    assert cache.api_key
    metrics = MetricsRecorder()
    await cache.get(cache.api_key, metrics.telemetry)

    def fail_start(_self: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    clock.t += 300
    with monkeypatch.context() as m:
        m.setattr(threading.Thread, "start", fail_start)
        assert await cache.get(cache.api_key, metrics.telemetry) == "key-1"
    assert cache.api_key.in_flight is None
    lookups = [a["result"] for n, _, a in metrics.updates() if n == METRIC_CACHE_LOOKUP]
    assert lookups == ["miss", "miss_with_fallback"]


def test_fetch_outlives_event_loop():
    """A fetch started on one loop that times out and closes can be picked up on
    a later loop, as for consecutive Lambda invocations.
    """
    fetcher = FakeFetcher()
    fetcher.set("key-id", "key-1", "v1")
    fetcher.gate = threading.Event()
    cache, _ = _new_cache(fetcher, SecretIDs(api_key="key-id"))
    assert cache.api_key
    entry = cache.api_key
    tel = MetricsRecorder().telemetry
    cache.fetch_timeout = 0.05

    async def get() -> str:
        return await cache.get(entry, tel)

    with pytest.raises(
        TimeoutError, match="waiting for AWS Secrets Manager secret fetch"
    ):
        asyncio.run(get())
    # Captured before the fetch can finish and clear it
    fut = entry.in_flight
    assert fut is not None

    # The fetch finishes after its loop has closed
    fetcher.gate.set()
    fut.result(timeout=5)
    assert asyncio.run(get()) == "key-1"
    assert fetcher.calls["key-id"] == 1

    # A refresh that's still running when its loop closes keeps serving the
    # stale value to later loops until it finishes
    fetcher.set("key-id", "key-2", "v2")
    fetcher.gate.clear()
    cache.refresh_interval = 0
    assert asyncio.run(get()) == "key-1"
    fut = entry.in_flight
    assert fut is not None
    assert asyncio.run(get()) == "key-1"
    fetcher.gate.set()
    assert fut.result(timeout=5) == "key-2"
    assert entry.value == "key-2"
    # Checked once the fetch is done, since its thread counts the call when it
    # starts fetching
    assert fetcher.calls["key-id"] == 2


async def test_client_cert_bundled():
    cert_key = new_cert_key()
    fetcher = FakeFetcher()
    fetcher.set("bundle", cert_key.cert + cert_key.key, "v1")
    cache, _ = _new_cache(fetcher, SecretIDs(client_cert="bundle", client_key="bundle"))
    assert cache.client_cert
    assert len(cache.client_cert.refs) == 1
    cert, key = await cache.get(cache.client_cert, MetricsRecorder().telemetry)
    assert cert == cert_key.cert
    assert key == cert_key.key
    assert fetcher.calls["bundle"] == 1


async def test_client_cert_separate(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO, logger=LOGGER)
    cert_key = new_cert_key()
    fetcher = FakeFetcher()
    fetcher.set("cert", cert_key.cert, "cv1")
    fetcher.set("key", cert_key.key, "kv1")
    cache, _ = _new_cache(fetcher, SecretIDs(client_cert="cert", client_key="key"))
    assert cache.client_cert
    metrics = MetricsRecorder()
    assert await cache.get(cache.client_cert, metrics.telemetry) == (
        cert_key.cert,
        cert_key.key,
    )
    # Reported as one entry, labeled as the client cert
    assert [
        a["secret_type"] for n, _, a in metrics.updates() if n == METRIC_CACHE_LOOKUP
    ] == ["tls_client_cert"]
    # But logged per secret
    assert [r.getMessage() for r in _records(caplog)] == [
        "Fetched AWS Secrets Manager secret (secret_id=cert, secret_type=tls_client_cert, fetch_path=sdk, version_id=cv1)",
        "Fetched AWS Secrets Manager secret (secret_id=key, secret_type=tls_client_key, fetch_path=sdk, version_id=kv1)",
    ]


@pytest.mark.parametrize(
    "ids,values,error",
    [
        (
            SecretIDs(client_cert="cert", client_key="key"),
            {"cert": b"not a cert", "key": b""},
            'no PEM certificate found in TLS client cert secret "cert"',
        ),
        (
            SecretIDs(client_cert="cert", client_key="key"),
            {"cert": new_cert_key().cert, "key": b"not a key"},
            'no PEM private key found in TLS client key secret "key"',
        ),
        (
            SecretIDs(server_ca_cert="ca"),
            {"ca": b"not a cert"},
            'no PEM certificates found in TLS server CA cert secret "ca"',
        ),
        (
            SecretIDs(api_key="key-id"),
            {"key-id": b"\xff"},
            "API key secret is not valid UTF-8",
        ),
    ],
)
async def test_invalid_secret(ids: SecretIDs, values: dict[str, bytes], error: str):
    fetcher = FakeFetcher()
    for secret_id, value in values.items():
        fetcher.set(secret_id, value, "v1")
    cache, _ = _new_cache(fetcher, ids)
    entry: CacheEntry[Any] | None = (
        cache.api_key or cache.client_cert or cache.server_ca_cert
    )
    assert entry
    metrics = MetricsRecorder()
    with pytest.raises(SecretFetchError, match=error):
        await cache.get(entry, metrics.telemetry)
    assert [
        a["failure_reason"] for n, _, a in metrics.updates() if n == METRIC_FETCH_FAILED
    ] == ["invalid_response"]


async def test_server_ca_cert():
    ca1, ca2 = new_cert_key("ca1"), new_cert_key("ca2")
    fetcher = FakeFetcher()
    # Non-certificate blocks are dropped
    fetcher.set("ca", ca1.cert + ca1.key + ca2.cert, "v1")
    cache, _ = _new_cache(fetcher, SecretIDs(server_ca_cert="ca"))
    assert cache.server_ca_cert
    assert (
        await cache.get(cache.server_ca_cert, MetricsRecorder().telemetry)
        == ca1.cert + ca2.cert
    )
