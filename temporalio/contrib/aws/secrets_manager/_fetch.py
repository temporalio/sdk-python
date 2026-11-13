"""Fetchers that retrieve secret values from AWS Secrets Manager.

Fetchers are synchronous and called from a worker thread, so that the cache that
uses them isn't bound to any event loop.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Protocol

from temporalio.contrib.aws.secrets_manager._telemetry import (
    FetchPath,
    InvalidResponseError,
    LambdaExtensionStatusError,
)

ENV_LAMBDA_EXTENSION_PORT = "PARAMETERS_SECRETS_EXTENSION_HTTP_PORT"
_ENV_LAMBDA_FUNCTION_NAME = "AWS_LAMBDA_FUNCTION_NAME"
_ENV_AWS_SESSION_TOKEN = "AWS_SESSION_TOKEN"
_LAMBDA_EXTENSION_TOKEN_HEADER = "X-Aws-Parameters-Secrets-Token"
_LAMBDA_EXTENSION_HOST = "localhost"
_DEFAULT_LAMBDA_EXTENSION_PORT = "2773"
# Probing a local port is either accepted or refused almost immediately, so this
# only bounds pathological cases
_LAMBDA_EXTENSION_PROBE_TIMEOUT_SECONDS = 0.01

# Bounds each request made by the fetchers this package creates
FETCH_TIMEOUT_SECONDS = 10.0


class SecretsClient(Protocol):
    """The subset of a boto3 Secrets Manager client used to fetch secrets.

    Implemented by ``boto3.client("secretsmanager")``. Must be safe to call from
    multiple threads, as boto3 clients are.
    """

    def get_secret_value(self, *, SecretId: str) -> Any:  # noqa: N803
        """Return the ``GetSecretValue`` response for the given secret ID."""
        ...


@dataclass(frozen=True)
class SecretValue:
    """A fetched secret value."""

    value: bytes
    version_id: str


class Fetcher(ABC):
    """Fetches secret values by ID. Must be safe to call from multiple threads."""

    @abstractmethod
    def fetch(self, secret_id: str) -> SecretValue:
        """Fetch the current value of the given secret."""

    @property
    @abstractmethod
    def path(self) -> FetchPath:
        """The fetch_path metric label for this fetcher."""


def new_default_fetcher() -> Fetcher:
    """Return a fetcher for the Lambda extension if it is detected, and otherwise
    an SDK fetcher with a client created from the default AWS config.

    The extension doesn't set PARAMETERS_SECRETS_EXTENSION_HTTP_PORT in the
    function's environment unless the user configured it, so if it is unset and
    running in Lambda, the extension's default port is probed. Setting it to empty
    disables detection, for when another service uses the default port.
    """
    port = os.environ.get(ENV_LAMBDA_EXTENSION_PORT)
    if port is None:
        # Only try to auto-detect the Lambda extension if we're in a Lambda
        # function
        if not os.environ.get(_ENV_LAMBDA_FUNCTION_NAME):
            return SDKFetcher()
        try:
            socket.create_connection(
                (_LAMBDA_EXTENSION_HOST, int(_DEFAULT_LAMBDA_EXTENSION_PORT)),
                timeout=_LAMBDA_EXTENSION_PROBE_TIMEOUT_SECONDS,
            ).close()
        except OSError:
            return SDKFetcher()
        port = _DEFAULT_LAMBDA_EXTENSION_PORT
    if not port:
        return SDKFetcher()
    return LambdaExtensionFetcher(port)


def boto3_available() -> bool:
    """Whether boto3 can be imported."""
    return importlib.util.find_spec("boto3") is not None


class SDKFetcher(Fetcher):
    """Fetches secrets directly from Secrets Manager. If no client is given, one is
    created from the default AWS config on first use.
    """

    def __init__(self, client: SecretsClient | None = None) -> None:
        """Create the fetcher."""
        self._lock = threading.Lock()
        self._client = client

    @property
    def path(self) -> FetchPath:  # noqa: D102
        return FetchPath.SDK

    def fetch(self, secret_id: str) -> SecretValue:  # noqa: D102
        resp = self._get_client().get_secret_value(SecretId=secret_id)
        return _new_secret_value(
            resp.get("SecretString"), resp.get("SecretBinary"), resp.get("VersionId")
        )

    def _get_client(self) -> SecretsClient:
        with self._lock:
            if self._client is None:
                # Imported lazily, since boto3 is only needed for this fetcher
                import boto3.session
                import botocore.config

                # A new session rather than boto3's default session, which isn't
                # thread-safe
                self._client = boto3.session.Session().client(
                    "secretsmanager",
                    config=botocore.config.Config(
                        connect_timeout=FETCH_TIMEOUT_SECONDS,
                        read_timeout=FETCH_TIMEOUT_SECONDS,
                    ),
                )
            return self._client


class LambdaExtensionFetcher(Fetcher):
    """Fetches secrets through the AWS Parameters and Secrets Lambda Extension. See
    https://docs.aws.amazon.com/secretsmanager/latest/userguide/retrieving-secrets_lambda.html.
    """

    def __init__(self, port: str) -> None:
        """Create a fetcher for the extension on the given port."""
        self._endpoint = (
            f"http://{_LAMBDA_EXTENSION_HOST}:{port}/secretsmanager/get?secretId="
        )
        # No proxy handler, so that HTTP(S)_PROXY env vars don't apply to requests
        # to the local extension
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @property
    def path(self) -> FetchPath:  # noqa: D102
        return FetchPath.EXTENSION

    def fetch(self, secret_id: str) -> SecretValue:  # noqa: D102
        req = urllib.request.Request(
            self._endpoint + urllib.parse.quote(secret_id, safe=""),
            headers={
                _LAMBDA_EXTENSION_TOKEN_HEADER: os.environ.get(
                    _ENV_AWS_SESSION_TOKEN, ""
                )
            },
        )
        try:
            with self._opener.open(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
                body: bytes = resp.read()
        except urllib.error.HTTPError as err:
            raise LambdaExtensionStatusError(
                err.code, err.read().decode(errors="replace")
            ) from None
        try:
            # Same shape as the GetSecretValue response, with SecretBinary base64
            out = json.loads(body)
            secret_binary = out.get("SecretBinary")
            return _new_secret_value(
                out.get("SecretString"),
                base64.b64decode(secret_binary) if secret_binary else None,
                out.get("VersionId"),
            )
        except InvalidResponseError:
            raise
        except Exception as err:
            raise InvalidResponseError(
                f"failed decoding AWS Parameters and Secrets Lambda Extension response: {err}"
            ) from err


def _new_secret_value(
    secret_string: str | None, secret_binary: bytes | None, version_id: str | None
) -> SecretValue:
    if secret_string is not None:
        value = secret_string.encode()
    elif secret_binary:
        value = secret_binary
    else:
        raise InvalidResponseError("secret has no value")
    return SecretValue(value=value, version_id=version_id or "")
