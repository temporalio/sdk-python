"""Metric and log helpers. Metric and label names must be the same across SDKs."""

from __future__ import annotations

import socket
import urllib.error
from collections.abc import Mapping
from enum import Enum
from typing import Any

import temporalio.common

METRIC_FETCH_FAILED = "temporal_aws_secrets_manager_fetch_failed"
METRIC_FETCH_LATENCY = "temporal_aws_secrets_manager_fetch_latency"
METRIC_CACHE_LOOKUP = "temporal_aws_secrets_manager_cache_lookup"

LABEL_SECRET_TYPE = "secret_type"
LABEL_FETCH_PATH = "fetch_path"
LABEL_FAILURE_REASON = "failure_reason"
LABEL_FALLBACK = "fallback"
LABEL_OUTCOME = "outcome"
LABEL_RESULT = "result"


class SecretType(str, Enum):
    """secret_type label values. Client cert and key secrets are fetched together
    and reported as TLS_CLIENT_CERT in metrics.
    """

    API_KEY = "api_key"
    TLS_CLIENT_CERT = "tls_client_cert"
    TLS_CLIENT_KEY = "tls_client_key"
    TLS_SERVER_CA_CERT = "tls_server_ca_cert"


class FetchPath(str, Enum):
    """fetch_path label values."""

    EXTENSION = "extension"
    """AWS Parameters and Secrets Lambda Extension"""
    SDK = "sdk"
    """Direct fetch from AWS Secrets Manager via the AWS SDK"""


class Fallback(str, Enum):
    """fallback label values."""

    STALE = "stale"
    NONE = "none"


class Outcome(str, Enum):
    """outcome label values."""

    SUCCESS = "success"
    FAILURE = "failure"


class LookupResult(str, Enum):
    """result label values."""

    HIT = "hit"
    MISS = "miss"
    MISS_WITH_FALLBACK = "miss_with_fallback"


class FailureReason(str, Enum):
    """failure_reason label values."""

    RESOURCE_NOT_FOUND = "resource_not_found"
    ACCESS_DENIED = "access_denied"
    DECRYPTION_FAILURE = "decryption_failure"
    INVALID_REQUEST = "invalid_request"
    THROTTLING = "throttling"
    INTERNAL_SERVICE_ERROR = "internal_service_error"
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"
    # The Lambda extension doesn't return the underlying AWS error, only an HTTP
    # status with a generic message, so its errors are only classified by status
    # class
    CLIENT_ERROR = "client_error"
    SERVER_ERROR = "server_error"
    OTHER = "other"


# AWS error codes mapped to failure reasons. Other codes are OTHER.
_AWS_ERROR_CODE_FAILURE_REASONS = {
    "ResourceNotFoundException": FailureReason.RESOURCE_NOT_FOUND,
    "AccessDeniedException": FailureReason.ACCESS_DENIED,
    "DecryptionFailure": FailureReason.DECRYPTION_FAILURE,
    "InvalidRequestException": FailureReason.INVALID_REQUEST,
    "InvalidParameterException": FailureReason.INVALID_REQUEST,
    "ThrottlingException": FailureReason.THROTTLING,
    "InternalServiceError": FailureReason.INTERNAL_SERVICE_ERROR,
}

# botocore timeout exception class names, matched by name so that botocore
# needn't be importable
_BOTOCORE_TIMEOUT_ERRORS = {"ConnectTimeoutError", "ReadTimeoutError"}


class InvalidResponseError(Exception):
    """A response that was received but has no usable secret value."""


class LambdaExtensionStatusError(Exception):
    """A non-200 response from the Lambda extension."""

    def __init__(self, status: int, body: str) -> None:
        """Create the error from the response status and body."""
        super().__init__(
            f"AWS Parameters and Secrets Lambda Extension returned status {status}: {body}"
        )
        self.status = status


def classify_failure(err: BaseException) -> FailureReason:
    """Return the failure_reason metric label for a fetch error."""
    if isinstance(err, InvalidResponseError):
        return FailureReason.INVALID_RESPONSE
    if isinstance(err, LambdaExtensionStatusError):
        if 400 <= err.status < 500:
            return FailureReason.CLIENT_ERROR
        if 500 <= err.status < 600:
            return FailureReason.SERVER_ERROR
        return FailureReason.OTHER
    # botocore.exceptions.ClientError, duck-typed so that botocore needn't be
    # importable
    response: Any = getattr(err, "response", None)
    if isinstance(response, Mapping):
        error: Any = response.get("Error")  # type: ignore[reportUnknownMemberType]
        code: Any = error.get("Code") if isinstance(error, Mapping) else None  # type: ignore[reportUnknownMemberType]
        return _AWS_ERROR_CODE_FAILURE_REASONS.get(code, FailureReason.OTHER)  # type: ignore[reportUnknownArgumentType]
    if isinstance(err, urllib.error.URLError) and isinstance(err.reason, BaseException):
        return classify_failure(err.reason)
    if isinstance(err, (TimeoutError, socket.timeout)) or any(
        cls.__name__ in _BOTOCORE_TIMEOUT_ERRORS for cls in type(err).__mro__
    ):
        return FailureReason.TIMEOUT
    return FailureReason.OTHER


class Telemetry:
    """Metric instruments for a client's metric meter."""

    def __init__(self, meter: temporalio.common.MetricMeter) -> None:
        """Create instruments on the given meter."""
        self.fetch_failed = meter.create_counter(
            METRIC_FETCH_FAILED,
            "Failed fetches of AWS Secrets Manager secrets",
        )
        self.fetch_latency = meter.create_histogram_timedelta(
            METRIC_FETCH_LATENCY,
            "Latency of AWS Secrets Manager secret fetches",
            "duration",
        )
        self.cache_lookup = meter.create_counter(
            METRIC_CACHE_LOOKUP,
            "Lookups of cached AWS Secrets Manager secrets that returned a value",
        )
