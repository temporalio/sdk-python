"""TTL cache of configured credentials.

The cache isn't bound to an event loop, so that it can be shared across clients
created under different loops (e.g. the per-invocation loops of a Lambda worker).
Fetches run in daemon threads, state is guarded by thread locks, and callers wait
for a fetch on their own loop.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Generic, TypeVar, cast

from temporalio.contrib.aws.secrets_manager._fetch import (
    FETCH_TIMEOUT_SECONDS,
    Fetcher,
    SecretValue,
)
from temporalio.contrib.aws.secrets_manager._telemetry import (
    LABEL_FAILURE_REASON,
    LABEL_FALLBACK,
    LABEL_FETCH_PATH,
    LABEL_OUTCOME,
    LABEL_RESULT,
    LABEL_SECRET_TYPE,
    FailureReason,
    Fallback,
    InvalidResponseError,
    LookupResult,
    Outcome,
    SecretType,
    Telemetry,
    classify_failure,
)

logger = logging.getLogger("temporalio.contrib.aws.secrets_manager")

# After a failed refresh falls back to a stale value, wait this long before
# trying again so that an outage doesn't add a fetch attempt to every credential
# lookup.
FAILED_REFRESH_RETRY_INTERVAL_SECONDS = 30.0

_T = TypeVar("_T")

_CERT_PEM_RE = re.compile(
    rb"-----BEGIN CERTIFICATE-----\r?\n.*?-----END CERTIFICATE-----", re.DOTALL
)
_KEY_PEM_RE = re.compile(
    rb"-----BEGIN ((?:RSA |EC )?PRIVATE KEY)-----\r?\n.*?-----END \1-----", re.DOTALL
)


class SecretFetchError(RuntimeError):
    """A failed fetch of one or more secrets, with its failure_reason label."""

    def __init__(self, message: str, reason: FailureReason) -> None:
        """Create the error."""
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class SecretIDs:
    """Configured secret IDs. Empty strings are unset."""

    api_key: str = ""
    client_cert: str = ""
    client_key: str = ""
    server_ca_cert: str = ""

    def has_tls(self) -> bool:
        """Whether any TLS secret ID is set."""
        return bool(self.client_cert or self.client_key or self.server_ca_cert)

    def is_empty(self) -> bool:
        """Whether no secret ID is set."""
        return not (self.api_key or self.has_tls())


@dataclass(frozen=True)
class _SecretRef:
    id: str
    secret_type: SecretType


class CacheEntry(Generic[_T]):
    """One or more secrets that are fetched and replaced together, cached as a
    single parsed value.
    """

    def __init__(
        self, refs: Sequence[_SecretRef], parse: Callable[[list[SecretValue]], _T]
    ) -> None:
        """Create an entry for the given secrets.

        ``refs`` are distinct secret IDs, and ``refs[0].secret_type`` is used as
        the secret_type metric label. ``parse`` is called on newly fetched
        secrets (in ``refs`` order) to produce the cached value, and should raise
        :py:class:`InvalidResponseError` if they're invalid.
        """
        self.refs = list(refs)
        self.parse = parse
        # Guards the fields below
        self.lock = threading.Lock()
        # The fetch in progress, or None if none
        self.in_flight: concurrent.futures.Future[_T] | None = None
        # In refs order, None until the first successful fetch
        self.version_ids: list[str] | None = None
        self.value: _T | None = None
        self.fetched_at = 0.0
        # Last failed fetch since the last successful fetch, or None if none
        self.failed_at: float | None = None


class SecretCache:
    """TTL cache of the configured credentials that falls back to the last fetched
    value, up to ``max_staleness`` old, when a refresh fails.
    """

    def __init__(
        self,
        fetcher: Fetcher,
        ids: SecretIDs,
        refresh_interval: timedelta,
        max_staleness: timedelta,
    ) -> None:
        """Create a cache for the given secret IDs."""
        self.fetcher = fetcher
        self.refresh_interval = refresh_interval.total_seconds()
        self.max_staleness = max_staleness.total_seconds()
        # How long callers wait for a fetch before falling back or failing
        self.fetch_timeout = FETCH_TIMEOUT_SECONDS
        # Monotonic seconds, overridable in tests
        self.now: Callable[[], float] = time.monotonic

        self.api_key: CacheEntry[str] | None = None
        if ids.api_key:
            self.api_key = CacheEntry(
                [_SecretRef(ids.api_key, SecretType.API_KEY)], _parse_api_key
            )
        # Client cert and key, fetched and replaced together to prevent
        # (cache-level) tearing on rotations
        self.client_cert: CacheEntry[tuple[bytes, bytes]] | None = None
        if ids.client_cert:
            self.client_cert = _new_client_cert_entry(ids.client_cert, ids.client_key)
        self.server_ca_cert: CacheEntry[bytes] | None = None
        if ids.server_ca_cert:
            self.server_ca_cert = _new_server_ca_cert_entry(ids.server_ca_cert)

    async def get(self, entry: CacheEntry[_T], tel: Telemetry) -> _T:
        """Get an entry's value, fetching it if needed.

        Returned values are shared across callers and must not be mutated.
        """
        base_attrs = {
            LABEL_SECRET_TYPE: entry.refs[0].secret_type.value,
            LABEL_FETCH_PATH: self.fetcher.path.value,
        }

        def record_lookup(result: LookupResult) -> None:
            tel.cache_lookup.add(1, {**base_attrs, LABEL_RESULT: result.value})

        # Check for a usable value in the cache
        with entry.lock:
            now = self.now()
            has_value = entry.version_ids is not None
            can_use_stale = has_value and now - entry.fetched_at < self.max_staleness
            stale = cast(_T, entry.value)
            result: LookupResult | None = None
            fut: concurrent.futures.Future[_T] | None = None
            start_fetch = False
            if (has_value and now - entry.fetched_at < self.refresh_interval) or (
                # Another caller is refreshing, so the current value is still the
                # latest known one
                can_use_stale and entry.in_flight is not None
            ):
                result = LookupResult.HIT
            elif (
                can_use_stale
                and entry.failed_at is not None
                and now - entry.failed_at < FAILED_REFRESH_RETRY_INTERVAL_SECONDS
            ):
                result = LookupResult.MISS_WITH_FALLBACK
            else:
                # Wait for a fetch, starting one if none is in progress
                fut = entry.in_flight
                if fut is None:
                    fut = concurrent.futures.Future()
                    # Marked running so that it can't be cancelled, e.g. when a
                    # caller's wait (via asyncio.wrap_future) times out or is
                    # cancelled
                    fut.set_running_or_notify_cancel()
                    entry.in_flight = fut
                    start_fetch = True
        if result is not None:
            record_lookup(result)
            return stale
        assert fut is not None

        if start_fetch:
            try:
                # A separate thread rather than a task, so that the fetch and the
                # cache's failure state aren't affected by the caller's
                # cancellation or event loop shutting down
                threading.Thread(
                    target=self._refresh,
                    args=(entry, fut, now, can_use_stale, tel, base_attrs),
                    name="temporalio-aws-secrets-manager-fetch",
                    daemon=True,
                ).start()
            except BaseException as err:
                # Fail this fetch, including for callers already waiting on it, so
                # that the next caller starts a new one
                with entry.lock:
                    entry.in_flight = None
                fut.set_exception(err)
                if not isinstance(err, Exception):
                    raise

        try:
            value = await asyncio.wait_for(asyncio.wrap_future(fut), self.fetch_timeout)
        except Exception as err:
            if can_use_stale:
                record_lookup(LookupResult.MISS_WITH_FALLBACK)
                return stale
            if isinstance(err, TimeoutError):
                raise TimeoutError(
                    f"timed out after {self.fetch_timeout}s waiting for AWS Secrets Manager secret fetch"
                ) from None
            raise
        record_lookup(LookupResult.MISS)
        return value

    def _refresh(
        self,
        entry: CacheEntry[_T],
        fut: concurrent.futures.Future[_T],
        start: float,
        can_use_stale: bool,
        tel: Telemetry,
        base_attrs: dict[str, str],
    ) -> None:
        """Fetch an entry's secrets and record the outcome in the cache."""
        result: tuple[_T, list[str]] | SecretFetchError
        try:
            result = self._fetch(entry)
        except SecretFetchError as err:
            result = err
        except Exception as err:  # Shouldn't happen, but don't leave fut pending
            result = SecretFetchError(str(err), FailureReason.OTHER)
            result.__cause__ = err
        latency = timedelta(seconds=self.now() - start)

        with entry.lock:
            entry.in_flight = None
            prev_version_ids = entry.version_ids
            prev_fetched_at = entry.fetched_at
            prev_failed = entry.failed_at is not None
            if isinstance(result, SecretFetchError):
                entry.failed_at = start
            else:
                entry.value, entry.version_ids = result
                entry.fetched_at = start
                entry.failed_at = None

        # Handle telemetry outside the lock, and resolve fut last so that the
        # fetch's metrics and logs are recorded by the time waiters return
        try:
            if isinstance(result, SecretFetchError):
                self._record_failure(
                    entry,
                    result,
                    latency,
                    start - prev_fetched_at,
                    can_use_stale,
                    tel,
                    base_attrs,
                )
            else:
                self._record_success(
                    entry,
                    result[1],
                    prev_version_ids,
                    prev_failed,
                    latency,
                    tel,
                    base_attrs,
                )
        except Exception:
            logger.exception("Failed recording AWS Secrets Manager telemetry")
        finally:
            if isinstance(result, SecretFetchError):
                fut.set_exception(result)
            else:
                fut.set_result(result[0])

    def _record_failure(
        self,
        entry: CacheEntry[_T],
        err: SecretFetchError,
        latency: timedelta,
        age: float,
        can_use_stale: bool,
        tel: Telemetry,
        base_attrs: dict[str, str],
    ) -> None:
        tel.fetch_latency.record(
            latency, {**base_attrs, LABEL_OUTCOME: Outcome.FAILURE.value}
        )
        tel.fetch_failed.add(
            1,
            {
                **base_attrs,
                LABEL_FAILURE_REASON: err.reason.value,
                LABEL_FALLBACK: (
                    Fallback.STALE if can_use_stale else Fallback.NONE
                ).value,
            },
        )
        if can_use_stale:
            for ref in entry.refs:
                logger.warning(
                    "Failed refreshing AWS Secrets Manager secret, using previously fetched value "
                    "(secret_id=%s, secret_type=%s, error=%s, age=%s, max_staleness=%s)",
                    ref.id,
                    ref.secret_type.value,
                    err,
                    timedelta(seconds=age),
                    timedelta(seconds=self.max_staleness),
                )

    def _record_success(
        self,
        entry: CacheEntry[_T],
        version_ids: list[str],
        prev_version_ids: list[str] | None,
        prev_failed: bool,
        latency: timedelta,
        tel: Telemetry,
        base_attrs: dict[str, str],
    ) -> None:
        tel.fetch_latency.record(
            latency, {**base_attrs, LABEL_OUTCOME: Outcome.SUCCESS.value}
        )
        for i, ref in enumerate(entry.refs):
            if prev_version_ids is None:
                logger.info(
                    "Fetched AWS Secrets Manager secret "
                    "(secret_id=%s, secret_type=%s, fetch_path=%s, version_id=%s)",
                    ref.id,
                    ref.secret_type.value,
                    self.fetcher.path.value,
                    version_ids[i],
                )
            elif prev_failed or version_ids[i] != prev_version_ids[i]:
                logger.info(
                    "Fetched updated AWS Secrets Manager secret "
                    "(secret_id=%s, secret_type=%s, previous_version_id=%s, version_id=%s)",
                    ref.id,
                    ref.secret_type.value,
                    prev_version_ids[i],
                    version_ids[i],
                )

    def _fetch(self, entry: CacheEntry[_T]) -> tuple[_T, list[str]]:
        """Fetch all of an entry's secrets and parse them. Returns the parsed value
        and the version IDs in refs order.
        """
        secrets: list[SecretValue] = []
        for ref in entry.refs:
            try:
                secrets.append(self.fetcher.fetch(ref.id))
            except Exception as err:
                raise SecretFetchError(
                    f'failed fetching AWS Secrets Manager secret "{ref.id}": {err}',
                    classify_failure(err),
                ) from err
        try:
            value = entry.parse(secrets)
        except InvalidResponseError as err:
            raise SecretFetchError(str(err), FailureReason.INVALID_RESPONSE) from err
        return value, [s.version_id for s in secrets]


def _parse_api_key(secrets: list[SecretValue]) -> str:
    try:
        return secrets[0].value.decode()
    except UnicodeDecodeError as err:
        raise InvalidResponseError(
            f"API key secret is not valid UTF-8: {err.reason}"
        ) from None


def _pem_certs(data: bytes) -> bytes:
    return b"\n".join(_CERT_PEM_RE.findall(data)) + b"\n"


def _new_client_cert_entry(
    cert_id: str, key_id: str
) -> CacheEntry[tuple[bytes, bytes]]:
    """Entry for the client cert and key, which may be the same secret if bundled.
    Each secret's relevant PEM blocks are extracted, so a bundled secret can be
    used as both.
    """
    refs = [_SecretRef(cert_id, SecretType.TLS_CLIENT_CERT)]
    if key_id != cert_id:
        refs.append(_SecretRef(key_id, SecretType.TLS_CLIENT_KEY))

    def parse(secrets: list[SecretValue]) -> tuple[bytes, bytes]:
        cert_pem = secrets[0].value
        key_pem = secrets[-1].value
        if not _CERT_PEM_RE.search(cert_pem):
            raise InvalidResponseError(
                f'no PEM certificate found in TLS client cert secret "{cert_id}"'
            )
        key_match = _KEY_PEM_RE.search(key_pem)
        if not key_match:
            raise InvalidResponseError(
                f'no PEM private key found in TLS client key secret "{key_id}"'
            )
        return _pem_certs(cert_pem), key_match.group(0) + b"\n"

    return CacheEntry(refs, parse)


def _new_server_ca_cert_entry(secret_id: str) -> CacheEntry[bytes]:
    def parse(secrets: list[SecretValue]) -> bytes:
        if not _CERT_PEM_RE.search(secrets[0].value):
            raise InvalidResponseError(
                f'no PEM certificates found in TLS server CA cert secret "{secret_id}"'
            )
        return _pem_certs(secrets[0].value)

    return CacheEntry([_SecretRef(secret_id, SecretType.TLS_SERVER_CA_CERT)], parse)
