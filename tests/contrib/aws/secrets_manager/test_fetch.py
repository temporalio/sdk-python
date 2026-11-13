from __future__ import annotations

import base64
import json
import socket
import threading
import urllib.error
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import boto3
import botocore.exceptions
import pytest
from moto import mock_aws

from temporalio.contrib.aws.secrets_manager import _fetch
from temporalio.contrib.aws.secrets_manager._fetch import (
    LambdaExtensionFetcher,
    SDKFetcher,
    SecretValue,
    new_default_fetcher,
)
from temporalio.contrib.aws.secrets_manager._telemetry import (
    FailureReason,
    FetchPath,
    InvalidResponseError,
    LambdaExtensionStatusError,
    classify_failure,
)


class _ExtensionServer:
    """Fake AWS Parameters and Secrets Lambda Extension."""

    def __init__(self) -> None:
        self.responses: dict[str, tuple[int, bytes]] = {}
        # (path, secret ID, token) per request, checked by tests rather than by
        # the handler, since a failed assertion on the server thread wouldn't fail
        # the test
        self.requests: list[tuple[str, str, str | None]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                url = urlparse(self.path)
                secret_id = parse_qs(url.query).get("secretId", [""])[0]
                server.requests.append(
                    (
                        url.path,
                        secret_id,
                        self.headers.get("X-Aws-Parameters-Secrets-Token"),
                    )
                )
                status, body = server.responses.get(secret_id, (400, b"not found"))
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._server = HTTPServer(("localhost", 0), Handler)
        self.port = str(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def set(self, secret_id: str, body: dict[str, Any], status: int = 200) -> None:
        self.responses[secret_id] = (status, json.dumps(body).encode())

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def extension_server() -> Iterator[_ExtensionServer]:
    server = _ExtensionServer()
    yield server
    server.close()


def test_lambda_extension_fetcher(
    extension_server: _ExtensionServer, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AWS_SESSION_TOKEN", "session-token")
    # Proxy env vars must not apply to the local extension
    monkeypatch.setenv("HTTP_PROXY", "http://localhost:1")
    monkeypatch.setenv("http_proxy", "http://localhost:1")
    arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:my/secret+name"
    extension_server.set(arn, {"SecretString": "value", "VersionId": "v1"})
    extension_server.set(
        "binary",
        {"SecretBinary": base64.b64encode(b"\x00\x01").decode(), "VersionId": "v2"},
    )
    fetcher = LambdaExtensionFetcher(extension_server.port)
    assert fetcher.path == FetchPath.EXTENSION

    assert fetcher.fetch(arn) == SecretValue(value=b"value", version_id="v1")
    assert fetcher.fetch("binary") == SecretValue(value=b"\x00\x01", version_id="v2")
    assert extension_server.requests == [
        ("/secretsmanager/get", arn, "session-token"),
        ("/secretsmanager/get", "binary", "session-token"),
    ]


def test_lambda_extension_fetcher_errors(extension_server: _ExtensionServer):
    fetcher = LambdaExtensionFetcher(extension_server.port)

    with pytest.raises(LambdaExtensionStatusError) as status_err:
        fetcher.fetch("missing")
    assert status_err.value.status == 400
    assert "returned status 400: not found" in str(status_err.value)
    assert classify_failure(status_err.value) == FailureReason.CLIENT_ERROR

    extension_server.set("server-error", {}, status=500)
    with pytest.raises(LambdaExtensionStatusError) as status_err:
        fetcher.fetch("server-error")
    assert classify_failure(status_err.value) == FailureReason.SERVER_ERROR

    extension_server.responses["bad-json"] = (200, b"not json")
    with pytest.raises(InvalidResponseError, match="failed decoding"):
        fetcher.fetch("bad-json")

    extension_server.set("no-value", {"VersionId": "v1"})
    with pytest.raises(InvalidResponseError, match="secret has no value"):
        fetcher.fetch("no-value")


def test_lambda_extension_fetcher_connection_refused():
    with socket.socket() as s:
        s.bind(("localhost", 0))
        port = s.getsockname()[1]
    with pytest.raises(urllib.error.URLError) as err:
        LambdaExtensionFetcher(str(port)).fetch("id")
    assert classify_failure(err.value) == FailureReason.OTHER


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Isolated from local AWS config, which could otherwise point boto3 at an
    # endpoint that moto doesn't intercept
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "missing-config"))
    monkeypatch.setenv(
        "AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "missing-credentials")
    )
    for env in ("AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_SECRETS_MANAGER"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.mark.usefixtures("aws_env")
@pytest.mark.parametrize("with_client", [False, True])
def test_sdk_fetcher(with_client: bool):
    with mock_aws():
        client = boto3.client("secretsmanager")
        string_version = client.create_secret(Name="string", SecretString="value")[
            "VersionId"
        ]
        binary_version = client.create_secret(Name="binary", SecretBinary=b"\x00\x01")[
            "VersionId"
        ]

        fetcher = SDKFetcher(client if with_client else None)
        assert fetcher.path == FetchPath.SDK
        assert fetcher.fetch("string") == SecretValue(
            value=b"value", version_id=string_version
        )
        assert fetcher.fetch("binary") == SecretValue(
            value=b"\x00\x01", version_id=binary_version
        )

        with pytest.raises(botocore.exceptions.ClientError) as err:
            fetcher.fetch("missing")
        assert classify_failure(err.value) == FailureReason.RESOURCE_NOT_FOUND


@pytest.mark.parametrize(
    "code,reason",
    [
        ("ResourceNotFoundException", FailureReason.RESOURCE_NOT_FOUND),
        ("AccessDeniedException", FailureReason.ACCESS_DENIED),
        ("DecryptionFailure", FailureReason.DECRYPTION_FAILURE),
        ("InvalidRequestException", FailureReason.INVALID_REQUEST),
        ("InvalidParameterException", FailureReason.INVALID_REQUEST),
        ("ThrottlingException", FailureReason.THROTTLING),
        ("InternalServiceError", FailureReason.INTERNAL_SERVICE_ERROR),
        ("SomethingElse", FailureReason.OTHER),
    ],
)
def test_classify_aws_errors(code: str, reason: FailureReason):
    err = botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": "msg"}}, "GetSecretValue"
    )
    assert classify_failure(err) == reason


def test_classify_other_errors():
    assert classify_failure(TimeoutError()) == FailureReason.TIMEOUT
    assert (
        classify_failure(urllib.error.URLError(TimeoutError())) == FailureReason.TIMEOUT
    )
    assert (
        classify_failure(botocore.exceptions.ReadTimeoutError(endpoint_url="x"))
        == FailureReason.TIMEOUT
    )
    assert (
        classify_failure(botocore.exceptions.ConnectTimeoutError(endpoint_url="x"))
        == FailureReason.TIMEOUT
    )
    assert classify_failure(InvalidResponseError()) == FailureReason.INVALID_RESPONSE
    assert classify_failure(ValueError()) == FailureReason.OTHER


def test_default_fetcher_detection(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PARAMETERS_SECRETS_EXTENSION_HTTP_PORT", raising=False)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    with socket.socket() as listener:
        listener.bind(("localhost", 0))
        listener.listen()
        port = str(listener.getsockname()[1])
        monkeypatch.setattr(_fetch, "_DEFAULT_LAMBDA_EXTENSION_PORT", port)

        # Not in Lambda, so the default port isn't probed
        assert isinstance(new_default_fetcher(), SDKFetcher)

        # In Lambda, with something listening on the default port
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "my-function")
        fetcher = new_default_fetcher()
        assert isinstance(fetcher, LambdaExtensionFetcher)
        assert f"localhost:{port}/" in fetcher._endpoint

        # Detection disabled with an empty port
        monkeypatch.setenv("PARAMETERS_SECRETS_EXTENSION_HTTP_PORT", "")
        assert isinstance(new_default_fetcher(), SDKFetcher)

        # Explicit port, used without probing
        monkeypatch.setenv("PARAMETERS_SECRETS_EXTENSION_HTTP_PORT", "1234")
        fetcher = new_default_fetcher()
        assert isinstance(fetcher, LambdaExtensionFetcher)
        assert "localhost:1234/" in fetcher._endpoint

    # In Lambda, with nothing listening on the default port
    monkeypatch.delenv("PARAMETERS_SECRETS_EXTENSION_HTTP_PORT")
    assert isinstance(new_default_fetcher(), SDKFetcher)
