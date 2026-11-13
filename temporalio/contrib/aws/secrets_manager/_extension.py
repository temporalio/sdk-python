from __future__ import annotations

import asyncio
import dataclasses
import os
import re
import weakref
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import temporalio.client
import temporalio.runtime
import temporalio.service
from temporalio.contrib.aws.secrets_manager._cache import (
    FAILED_REFRESH_RETRY_INTERVAL_SECONDS,
    SecretCache,
    SecretIDs,
    logger,
)
from temporalio.contrib.aws.secrets_manager._fetch import (
    ENV_LAMBDA_EXTENSION_PORT,
    Fetcher,
    SDKFetcher,
    SecretsClient,
    boto3_available,
    new_default_fetcher,
)
from temporalio.contrib.aws.secrets_manager._telemetry import Telemetry
from temporalio.envconfig import (
    ClientConfigExtension,
    ClientConfigExtensionRequest,
    ClientConfigProfile,
)

EXTENSION_NAME = "aws_secrets_manager"
"""The name of the extension, which is also its table key under
``profile.<profile>.ext``."""

_PLUGIN_NAME = "temporalio.contrib.aws.secrets_manager"

_ENV_API_KEY_ID = "TEMPORAL_EXT_AWS_SECRETS_MANAGER_API_KEY_ID"
_ENV_CLIENT_CERT_ID = "TEMPORAL_EXT_AWS_SECRETS_MANAGER_TLS_CLIENT_CERT_ID"
_ENV_CLIENT_KEY_ID = "TEMPORAL_EXT_AWS_SECRETS_MANAGER_TLS_CLIENT_KEY_ID"
_ENV_SERVER_CA_CERT_ID = "TEMPORAL_EXT_AWS_SECRETS_MANAGER_TLS_SERVER_CA_CERT_ID"

# Same env var and default as the AWS Parameters and Secrets Lambda Extension's
# cache TTL
_ENV_SECRETS_MANAGER_TTL = "SECRETS_MANAGER_TTL"
_DEFAULT_REFRESH_INTERVAL = timedelta(minutes=5)
_DEFAULT_MAX_STALENESS = timedelta(hours=24)


@dataclass(frozen=True)
class SecretsManagerExtension(ClientConfigExtension):
    """Client config extension that loads client credentials from AWS Secrets
    Manager.

    Pass to
    :py:meth:`temporalio.envconfig.ClientConfig.load_client_connect_config`. See
    the :py:mod:`temporalio.contrib.aws.secrets_manager` module documentation for
    configuration details.

    .. warning::
       This API is experimental and unstable.
    """

    secrets_client: SecretsClient | None = None
    """Secrets Manager client used to fetch secrets, e.g.
    ``boto3.client("secretsmanager")``. Called from a worker thread. If unset,
    secrets are fetched through the AWS Parameters and Secrets Lambda Extension if
    it is detected, and otherwise with a client created from the default AWS
    config, which requires the ``boto3`` extra."""

    refresh_interval: timedelta | None = None
    """How long a fetched secret is used before it is refreshed. If unset,
    defaults to the ``SECRETS_MANAGER_TTL`` env var (non-negative whole seconds,
    where 0 refreshes on every use) as used by the AWS Parameters and Secrets
    Lambda Extension, or 5 minutes if that is unset."""

    max_staleness: timedelta | None = None
    """How long after its last successful fetch a secret may still be used if
    refreshing it fails. Defaults to 24 hours. Values less than the refresh
    interval are treated as the refresh interval."""

    def name(self) -> str:
        """The extension name, ``aws_secrets_manager``."""
        return EXTENSION_NAME

    def client_plugin(
        self, req: ClientConfigExtensionRequest
    ) -> temporalio.client.Plugin | None:
        """Read secret IDs from the profile and env, and return a plugin that
        fetches the configured secrets, or None if none are configured.
        """
        ids = _load_secret_ids(req)
        if ids.is_empty():
            return None

        refresh_interval = self._refresh_interval()
        max_staleness = self.max_staleness or _DEFAULT_MAX_STALENESS
        if max_staleness <= timedelta(0):
            max_staleness = _DEFAULT_MAX_STALENESS
        max_staleness = max(max_staleness, refresh_interval)

        fetcher: Fetcher
        if self.secrets_client is not None:
            fetcher = SDKFetcher(self.secrets_client)
        else:
            fetcher = new_default_fetcher()
            if isinstance(fetcher, SDKFetcher) and not boto3_available():
                raise RuntimeError(
                    "fetching secrets directly from AWS Secrets Manager requires boto3, "
                    "since the AWS Parameters and Secrets Lambda Extension was not detected "
                    f"(see {ENV_LAMBDA_EXTENSION_PORT}). Install the boto3 extra, e.g. "
                    "temporalio[boto3], or add the extension to your Lambda function"
                )
        return _SecretsManagerPlugin(
            ids, SecretCache(fetcher, ids, refresh_interval, max_staleness)
        )

    def _refresh_interval(self) -> timedelta:
        if self.refresh_interval is not None:
            if self.refresh_interval < timedelta(0):
                raise ValueError("refresh_interval must not be negative")
            return self.refresh_interval
        # Read from the process env rather than the config env, like
        # PARAMETERS_SECRETS_EXTENSION_HTTP_PORT, since this is Lambda runtime
        # config shared with the Lambda extension rather than Temporal client
        # config
        s = os.environ.get(_ENV_SECRETS_MANAGER_TTL)
        if not s:
            return _DEFAULT_REFRESH_INTERVAL
        if not re.fullmatch(r"[0-9]+", s):
            raise ValueError(
                f'invalid {_ENV_SECRETS_MANAGER_TTL} "{s}", must be a non-negative whole number of seconds'
            )
        try:
            return timedelta(seconds=int(s))
        except OverflowError:
            raise ValueError(
                f'invalid {_ENV_SECRETS_MANAGER_TTL} "{s}", must be a non-negative whole number of seconds'
            ) from None


def _load_secret_ids(req: ClientConfigExtensionRequest) -> SecretIDs:
    fields: dict[str, str] = {}
    _apply_toml(fields, req.profile.ext.get(EXTENSION_NAME) or {})
    if req.env is not None:
        # Like core envconfig, set-but-empty env vars override (and so can unset)
        # file values
        for env, field in (
            (_ENV_API_KEY_ID, "api_key"),
            (_ENV_CLIENT_CERT_ID, "client_cert"),
            (_ENV_CLIENT_KEY_ID, "client_key"),
            (_ENV_SERVER_CA_CERT_ID, "server_ca_cert"),
        ):
            if env in req.env:
                fields[field] = req.env[env]
    ids = SecretIDs(**fields)
    _validate(ids, req.profile)
    return ids


def _apply_toml(fields: dict[str, str], table: Mapping[str, Any]) -> None:
    for key, val in table.items():
        if key == "api_key_id":
            fields["api_key"] = _toml_string(key, val)
        elif key == "tls":
            if not isinstance(val, Mapping):
                raise ValueError(
                    f"ext.{EXTENSION_NAME}.tls must be a table, got {type(val).__name__}"
                )
            tls: Mapping[str, Any] = val
            for tls_key, tls_val in tls.items():
                if tls_key == "client_cert_id":
                    fields["client_cert"] = _toml_string(f"tls.{tls_key}", tls_val)
                elif tls_key == "client_key_id":
                    fields["client_key"] = _toml_string(f"tls.{tls_key}", tls_val)
                elif tls_key == "server_ca_cert_id":
                    fields["server_ca_cert"] = _toml_string(f"tls.{tls_key}", tls_val)
                else:
                    raise ValueError(
                        f'unknown key "{tls_key}" in ext.{EXTENSION_NAME}.tls'
                    )
        else:
            raise ValueError(f'unknown key "{key}" in ext.{EXTENSION_NAME}')


def _toml_string(key: str, val: Any) -> str:
    if not isinstance(val, str):
        raise ValueError(
            f"ext.{EXTENSION_NAME}.{key} must be a string, got {type(val).__name__}"
        )
    return val


def _validate(ids: SecretIDs, prof: ClientConfigProfile) -> None:
    conflicts: list[str] = []
    if ids.api_key and prof.api_key:
        conflicts.append("API key secret ID cannot be set with an API key")
    if prof.tls is not None:
        if ids.client_cert and prof.tls.client_cert:
            conflicts.append(
                "TLS client cert secret ID cannot be set with client cert path or data"
            )
        if ids.client_key and prof.tls.client_private_key:
            conflicts.append(
                "TLS client key secret ID cannot be set with client key path or data"
            )
        if ids.server_ca_cert and prof.tls.server_root_ca_cert:
            conflicts.append(
                "TLS server CA cert secret ID cannot be set with server CA cert path or data"
            )
        if ids.has_tls() and prof.tls.disabled:
            conflicts.append("TLS secret IDs cannot be set when TLS is disabled")
    if bool(ids.client_cert) != bool(ids.client_key):
        conflicts.append("TLS client cert and key secret IDs must be set together")
    if conflicts:
        raise ValueError(f"invalid config: {sorted(conflicts)}")


class _SecretsManagerPlugin(temporalio.client.Plugin):
    def __init__(self, ids: SecretIDs, cache: SecretCache) -> None:
        self._ids = ids
        self._cache = cache
        # Strong references to running API key refresh tasks
        self._refresh_tasks: set[asyncio.Task[None]] = set()

    def name(self) -> str:
        return _PLUGIN_NAME

    def configure_client(
        self, config: temporalio.client.ClientConfig
    ) -> temporalio.client.ClientConfig:
        return config

    async def connect_service_client(
        self,
        config: temporalio.service.ConnectConfig,
        next: Callable[
            [temporalio.service.ConnectConfig],
            Awaitable[temporalio.service.ServiceClient],
        ],
    ) -> temporalio.service.ServiceClient:
        runtime = config.runtime or temporalio.runtime.Runtime.default()
        tel = Telemetry(runtime.metric_meter)
        config = dataclasses.replace(config)

        if self._cache.api_key is not None:
            if config.api_key is not None:
                raise ValueError(
                    "cannot use API key from AWS Secrets Manager, API key already set"
                )
            # Like an API key from envconfig, this enables TLS unless it's
            # explicitly configured
            config.api_key = await self._cache.get(self._cache.api_key, tel)

        if self._ids.has_tls():
            if config.tls is False:
                raise ValueError(
                    "cannot use TLS config from AWS Secrets Manager, TLS is disabled"
                )
            tls = (
                config.tls
                if isinstance(config.tls, temporalio.service.TLSConfig)
                else temporalio.service.TLSConfig()
            )
            if self._cache.client_cert is not None:
                if tls.client_cert is not None or tls.client_private_key is not None:
                    raise ValueError(
                        "cannot use TLS client cert from AWS Secrets Manager, client certificates already set"
                    )
                cert, key = await self._cache.get(self._cache.client_cert, tel)
                tls = dataclasses.replace(tls, client_cert=cert, client_private_key=key)
            if self._cache.server_ca_cert is not None:
                if tls.server_root_ca_cert is not None:
                    raise ValueError(
                        "cannot use TLS server CA cert from AWS Secrets Manager, server root CA cert already set"
                    )
                ca_cert = await self._cache.get(self._cache.server_ca_cert, tel)
                tls = dataclasses.replace(tls, server_root_ca_cert=ca_cert)
            config.tls = tls

        service_client = await next(config)
        if config.api_key is not None and self._cache.api_key is not None:
            task = asyncio.create_task(
                self._refresh_api_key(weakref.ref(service_client), config.api_key, tel)
            )
            self._refresh_tasks.add(task)
            task.add_done_callback(self._refresh_tasks.discard)
        return service_client

    async def _refresh_api_key(
        self,
        service_client_ref: weakref.ref[temporalio.service.ServiceClient],
        api_key: str,
        tel: Telemetry,
    ) -> None:
        """Keep a service client's API key up to date while it is alive.

        The task belongs to the event loop the client was connected on, so it
        stops when that loop does.
        """
        entry = self._cache.api_key
        assert entry is not None
        # Checking more often than this would only be cache hits, and with a
        # refresh interval of 0 (no caching), this bounds the fetch rate
        interval = max(
            self._cache.refresh_interval, FAILED_REFRESH_RETRY_INTERVAL_SECONDS
        )
        while True:
            # Retry sooner if the last refresh failed
            failing = entry.failed_at is not None
            await asyncio.sleep(
                FAILED_REFRESH_RETRY_INTERVAL_SECONDS if failing else interval
            )
            if service_client_ref() is None:
                return
            try:
                new_key = await self._cache.get(entry, tel)
            except Exception:
                # Already logged and counted by the cache, and the current key
                # keeps being used
                continue
            if new_key != api_key:
                # Not held across awaits, so that the client can be collected
                service_client = service_client_ref()
                if service_client is None:
                    return
                service_client.update_api_key(new_key)
                del service_client
                api_key = new_key
                logger.debug("Updated client API key from AWS Secrets Manager")
