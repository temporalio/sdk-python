"""Shared test helpers for the AWS Secrets Manager extension."""

from __future__ import annotations

import asyncio
import datetime
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from temporalio.contrib.aws.secrets_manager._fetch import Fetcher, SecretValue
from temporalio.contrib.aws.secrets_manager._telemetry import FetchPath, Telemetry
from temporalio.runtime import MetricBuffer, Runtime, TelemetryConfig


async def wait_until(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    """Poll until ``cond`` is true, failing if it isn't within ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(0.01)


@dataclass(frozen=True)
class CertKey:
    cert: bytes
    key: bytes


def new_cert_key(name: str = "test") -> CertKey:
    """A self-signed PEM cert and key."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return CertKey(
        cert=cert.public_bytes(serialization.Encoding.PEM),
        key=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


class FakeFetcher(Fetcher):
    """Returns configured values, counting fetches. A value may be an exception to
    raise. If ``gate`` is set, fetches block until it is.
    """

    def __init__(self, path: FetchPath = FetchPath.SDK) -> None:
        self._path = path
        self._lock = threading.Lock()
        self.values: dict[str, SecretValue | Exception] = {}
        self.calls: dict[str, int] = {}
        self.gate: threading.Event | None = None
        self.on_fetch: Callable[[str], None] | None = None

    @property
    def path(self) -> FetchPath:
        return self._path

    def set(self, secret_id: str, value: bytes | str, version_id: str) -> None:
        if isinstance(value, str):
            value = value.encode()
        self.values[secret_id] = SecretValue(value=value, version_id=version_id)

    def fail(self, secret_id: str, err: Exception) -> None:
        self.values[secret_id] = err

    def fetch(self, secret_id: str) -> SecretValue:
        with self._lock:
            self.calls[secret_id] = self.calls.get(secret_id, 0) + 1
        if self.on_fetch:
            self.on_fetch(secret_id)
        if self.gate is not None:
            self.gate.wait()
        value = self.values[secret_id]
        if isinstance(value, Exception):
            raise value
        return value


class MetricsRecorder:
    """Telemetry backed by a real runtime's metric buffer."""

    def __init__(self) -> None:
        self.buffer = MetricBuffer(10000)
        self.runtime = Runtime(
            telemetry=TelemetryConfig(metrics=self.buffer, attach_service_name=False)
        )
        self.telemetry = Telemetry(self.runtime.metric_meter)

    def updates(self) -> list[tuple[str, int | float, dict[str, str]]]:
        """Drain the buffer, returning (name, value, attributes) per update."""
        return [
            (
                u.metric.name,
                u.value,
                {k: str(v) for k, v in u.attributes.items()},
            )
            for u in self.buffer.retrieve_updates()
        ]

    def counts(self, name: str) -> list[dict[str, str]]:
        """Drain the buffer, returning the attributes of each update of the given
        counter (each update is 1).
        """
        return [attrs for n, _, attrs in self.updates() if n == name]
