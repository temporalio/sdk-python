"""Tests for :py:attr:`temporalio.service.TLSConfig.verification_server_name`.

Each test runs an in-process TLS server whose certificate is valid only for
``pinned.test`` while the client always dials ``localhost``, so no Temporal
server is needed. The server records each handshake's outcome and the SNI it
received.
"""

from __future__ import annotations

import asyncio
import datetime
import socket
import ssl
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import temporalio.service

PINNED_NAME = "pinned.test"


def test_tls_config_verification_server_name_reaches_bridge():
    config = temporalio.service.TLSConfig(verification_server_name=PINNED_NAME)
    assert config._to_bridge_config().verification_server_name == PINNED_NAME
    default = temporalio.service.TLSConfig()
    assert default._to_bridge_config().verification_server_name is None


async def test_tls_verification_server_name_requires_root_ca():
    # The check is enforced when building connection options, before any dial.
    with pytest.raises(ValueError, match="server root CA cert"):
        await temporalio.service.ServiceClient.connect(
            temporalio.service.ConnectConfig(
                target_host="localhost:1",
                tls=temporalio.service.TLSConfig(verification_server_name=PINNED_NAME),
            )
        )


async def test_tls_default_verification_rejects_unmatched_name(tls_server: _TlsServer):
    # Baseline for the tests below: the certificate is only valid for the
    # pinned name, so verifying against the dialed host rejects it.
    handshake = await _handshake(
        tls_server, temporalio.service.TLSConfig(server_root_ca_cert=tls_server.ca_pem)
    )
    assert not handshake.ok


async def test_tls_verification_server_name_decouples_verification_from_sni(
    tls_server: _TlsServer,
):
    # Verification against the pinned name succeeds, while the SNI the server
    # sees is still the dialed host rather than the pinned name.
    handshake = await _handshake(
        tls_server,
        temporalio.service.TLSConfig(
            server_root_ca_cert=tls_server.ca_pem,
            verification_server_name=PINNED_NAME,
        ),
    )
    assert handshake.ok
    assert handshake.sni == "localhost"


async def test_tls_verification_server_name_is_enforced(tls_server: _TlsServer):
    # A pinned name the certificate does not carry is still rejected; the
    # option redirects verification rather than disabling it.
    handshake = await _handshake(
        tls_server,
        temporalio.service.TLSConfig(
            server_root_ca_cert=tls_server.ca_pem,
            verification_server_name="wrong.test",
        ),
    )
    assert not handshake.ok


@dataclass
class _Handshake:
    ok: bool
    sni: str | None


async def _handshake(
    server: _TlsServer, tls: temporalio.service.TLSConfig
) -> _Handshake:
    """Attempt a connection and return the server's view of the handshake."""
    config = temporalio.service.ConnectConfig(
        target_host=f"localhost:{server.port}", tls=tls
    )
    # The connect always fails since this server speaks no gRPC; only whether
    # the TLS handshake completed matters.
    try:
        await asyncio.wait_for(temporalio.service.ServiceClient.connect(config), 20)
    except asyncio.TimeoutError:
        raise
    except Exception:
        pass
    else:
        pytest.fail("connect unexpectedly succeeded")
    return await server.next_handshake()


class _TlsServer:
    """TLS server that records handshake outcomes and the SNI it receives."""

    def __init__(self, certs: Path) -> None:
        self.ca_pem = (certs / "ca.pem").read_bytes()
        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ctx.load_cert_chain(certs / "srv.pem", certs / "srv.key")
        self._ctx.set_alpn_protocols(["h2"])
        # Populated by the SNI callback for the connection currently
        # handshaking; the accept loop is single-threaded.
        self._sni: str | None = None

        def on_sni(
            _sock: ssl.SSLObject, name: str | None, _ctx: ssl.SSLContext
        ) -> None:
            self._sni = name

        self._ctx.sni_callback = on_sni
        self._handshakes: list[_Handshake] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock = socket.create_server(("127.0.0.1", 0))
        self._sock.settimeout(0.1)
        self.port: int = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            conn.settimeout(10)  # A stalled handshake must not wedge this loop.
            self._sni = None
            try:
                tls = self._ctx.wrap_socket(conn, server_side=True)
            except (ssl.SSLError, OSError):
                conn.close()
                self._record(_Handshake(ok=False, sni=self._sni))
                continue
            self._record(_Handshake(ok=True, sni=self._sni))
            tls.close()
        self._sock.close()

    def _record(self, handshake: _Handshake) -> None:
        with self._lock:
            self._handshakes.append(handshake)

    async def next_handshake(self) -> _Handshake:
        """Wait for and consume the next recorded handshake."""
        # The client can report its result before this thread has recorded
        # the outcome, so wait for the observation to land.
        for _ in range(200):
            with self._lock:
                if self._handshakes:
                    return self._handshakes.pop(0)
            await asyncio.sleep(0.05)
        raise AssertionError("server observed no TLS handshake")

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


@pytest_asyncio.fixture  # type: ignore[reportUntypedFunctionDecorator]
async def tls_server(tmp_path: Path) -> AsyncIterator[_TlsServer]:
    _write_pinned_certs(tmp_path)
    server = _TlsServer(tmp_path)
    try:
        yield server
    finally:
        server.close()


def _write_pinned_certs(path: Path) -> None:
    """Write a CA and a server cert (chained to it) valid only for ``pinned.test``."""
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=7))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    server_key = ec.generate_private_key(ec.SECP256R1())
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, PINNED_NAME)]))
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=7))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(PINNED_NAME)]), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    (path / "ca.pem").write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    (path / "srv.pem").write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    (path / "srv.key").write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
