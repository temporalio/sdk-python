from __future__ import annotations

import asyncio
import gc
import textwrap
import threading
from collections.abc import AsyncIterator, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import temporalio.client
from temporalio.contrib.aws.secrets_manager import (
    SecretsManagerExtension,
    _extension,
)
from temporalio.contrib.aws.secrets_manager._extension import _SecretsManagerPlugin
from temporalio.contrib.aws.secrets_manager._fetch import (
    LambdaExtensionFetcher,
    SDKFetcher,
)
from temporalio.contrib.aws.secrets_manager._telemetry import (
    METRIC_CACHE_LOOKUP,
)
from temporalio.envconfig import (
    ClientConfig,
    ClientConfigExtensionRequest,
    ClientConfigProfile,
    ClientConnectConfig,
)
from temporalio.service import ConnectConfig, ServiceClient, TLSConfig
from tests.contrib.aws.secrets_manager.helpers import (
    MetricsRecorder,
    new_cert_key,
    wait_until,
)


class _FakeSecretsClient:
    """Thread-safe stand-in for a boto3 Secrets Manager client."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.secrets: dict[str, tuple[bytes | str, str]] = {}
        self.calls: list[str] = []

    def set(self, secret_id: str, value: bytes | str, version_id: str) -> None:
        with self._lock:
            self.secrets[secret_id] = (value, version_id)

    def get_secret_value(self, *, SecretId: str) -> Any:  # noqa: N803
        with self._lock:
            self.calls.append(SecretId)
            value, version_id = self.secrets[SecretId]
        if isinstance(value, str):
            return {"SecretString": value, "VersionId": version_id}
        return {"SecretBinary": value, "VersionId": version_id}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in (
        "PARAMETERS_SECRETS_EXTENSION_HTTP_PORT",
        "AWS_LAMBDA_FUNCTION_NAME",
        "SECRETS_MANAGER_TTL",
    ):
        monkeypatch.delenv(env, raising=False)


def _load(
    tmp_path: Path,
    toml: str = "",
    env: Mapping[str, str] | None = None,
    extension: SecretsManagerExtension | None = None,
    disable_env: bool = False,
) -> ClientConnectConfig:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[profile.default]\naddress = "localhost:7233"\n' + textwrap.dedent(toml)
    )
    return ClientConfig.load_client_connect_config(
        config_file=str(config_file),
        override_env_vars=env or {},
        disable_env=disable_env,
        extensions=[
            extension or SecretsManagerExtension(secrets_client=_FakeSecretsClient())
        ],
    )


def _plugin(config: ClientConnectConfig) -> _SecretsManagerPlugin:
    plugins = config.get("plugins")
    assert plugins and len(plugins) == 1
    plugin = plugins[0]
    assert isinstance(plugin, _SecretsManagerPlugin)
    _plugins.append(plugin)
    return plugin


def test_no_secret_ids_no_plugin(tmp_path: Path):
    assert "plugins" not in _load(tmp_path)
    # Empty env vars unset file values
    assert "plugins" not in _load(
        tmp_path,
        """
        [profile.default.ext.aws_secrets_manager]
        api_key_id = "from-file"
        """,
        env={"TEMPORAL_EXT_AWS_SECRETS_MANAGER_API_KEY_ID": ""},
    )


def test_load_secret_ids(tmp_path: Path):
    toml = """
        [profile.default.ext.aws_secrets_manager]
        api_key_id = "api-key-file"
        [profile.default.ext.aws_secrets_manager.tls]
        client_cert_id = "cert-file"
        client_key_id = "key-file"
        server_ca_cert_id = "ca-file"
        """
    plugin = _plugin(_load(tmp_path, toml))
    assert plugin.name() == "temporalio.contrib.aws.secrets_manager"
    ids = plugin._ids
    assert (ids.api_key, ids.client_cert, ids.client_key, ids.server_ca_cert) == (
        "api-key-file",
        "cert-file",
        "key-file",
        "ca-file",
    )

    env = {
        "TEMPORAL_EXT_AWS_SECRETS_MANAGER_API_KEY_ID": "api-key-env",
        "TEMPORAL_EXT_AWS_SECRETS_MANAGER_TLS_CLIENT_CERT_ID": "bundle-env",
        "TEMPORAL_EXT_AWS_SECRETS_MANAGER_TLS_CLIENT_KEY_ID": "bundle-env",
        "TEMPORAL_EXT_AWS_SECRETS_MANAGER_TLS_SERVER_CA_CERT_ID": "ca-env",
    }
    ids = _plugin(_load(tmp_path, toml, env))._ids
    assert (ids.api_key, ids.client_cert, ids.client_key, ids.server_ca_cert) == (
        "api-key-env",
        "bundle-env",
        "bundle-env",
        "ca-env",
    )

    # Env ignored when disabled
    assert (
        _plugin(_load(tmp_path, toml, env, disable_env=True))._ids.api_key
        == "api-key-file"
    )


@pytest.mark.parametrize(
    "toml,env,error",
    [
        (
            """
            [profile.default.ext.aws_secrets_manager]
            unknown = "x"
            """,
            {},
            'unknown key "unknown" in ext.aws_secrets_manager',
        ),
        (
            """
            [profile.default.ext.aws_secrets_manager.tls]
            unknown = "x"
            """,
            {},
            'unknown key "unknown" in ext.aws_secrets_manager.tls',
        ),
        (
            """
            [profile.default.ext.aws_secrets_manager]
            api_key_id = 5
            """,
            {},
            "ext.aws_secrets_manager.api_key_id must be a string, got int",
        ),
        (
            """
            [profile.default.ext.aws_secrets_manager]
            tls = "x"
            """,
            {},
            "ext.aws_secrets_manager.tls must be a table, got str",
        ),
        (
            """
            [profile.default.ext.aws_secrets_manager.tls]
            client_cert_id = 5
            """,
            {},
            "ext.aws_secrets_manager.tls.client_cert_id must be a string, got int",
        ),
        (
            'api_key = "key"\n',
            {"TEMPORAL_EXT_AWS_SECRETS_MANAGER_API_KEY_ID": "id"},
            "API key secret ID cannot be set with an API key",
        ),
        (
            "",
            {
                "TEMPORAL_API_KEY": "key",
                "TEMPORAL_EXT_AWS_SECRETS_MANAGER_API_KEY_ID": "id",
            },
            "API key secret ID cannot be set with an API key",
        ),
        (
            "",
            {
                "TEMPORAL_TLS_CLIENT_CERT_DATA": "cert",
                "TEMPORAL_TLS_CLIENT_KEY_DATA": "key",
                "TEMPORAL_EXT_AWS_SECRETS_MANAGER_TLS_CLIENT_CERT_ID": "id",
                "TEMPORAL_EXT_AWS_SECRETS_MANAGER_TLS_CLIENT_KEY_ID": "id",
            },
            "TLS client cert secret ID cannot be set with client cert path or data', "
            "'TLS client key secret ID cannot be set with client key path or data",
        ),
        (
            """
            [profile.default.tls]
            server_ca_cert_data = "ca"
            [profile.default.ext.aws_secrets_manager.tls]
            server_ca_cert_id = "id"
            """,
            {},
            "TLS server CA cert secret ID cannot be set with server CA cert path or data",
        ),
        (
            """
            [profile.default.tls]
            disabled = true
            [profile.default.ext.aws_secrets_manager.tls]
            server_ca_cert_id = "id"
            """,
            {},
            "TLS secret IDs cannot be set when TLS is disabled",
        ),
        (
            "",
            {"TEMPORAL_EXT_AWS_SECRETS_MANAGER_TLS_CLIENT_CERT_ID": "id"},
            "TLS client cert and key secret IDs must be set together",
        ),
        (
            "",
            {"TEMPORAL_EXT_AWS_SECRETS_MANAGER_TLS_CLIENT_KEY_ID": "id"},
            "TLS client cert and key secret IDs must be set together",
        ),
    ],
)
def test_invalid_config(tmp_path: Path, toml: str, env: dict[str, str], error: str):
    with pytest.raises(RuntimeError) as err:
        _load(tmp_path, toml, env)
    assert str(err.value).startswith('client config extension "aws_secrets_manager": ')
    assert error in str(err.value)


def test_api_key_secret_with_tls_disabled_allowed(tmp_path: Path):
    plugin = _plugin(
        _load(
            tmp_path,
            """
            [profile.default.tls]
            disabled = true
            [profile.default.ext.aws_secrets_manager]
            api_key_id = "id"
            """,
        )
    )
    assert plugin._ids.api_key == "id"


def test_refresh_interval_and_max_staleness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    toml = """
        [profile.default.ext.aws_secrets_manager]
        api_key_id = "id"
        """

    def cache_settings(
        ext: SecretsManagerExtension | None = None,
    ) -> tuple[float, float]:
        cache = _plugin(_load(tmp_path, toml, extension=ext))._cache
        return cache.refresh_interval, cache.max_staleness

    client = _FakeSecretsClient()
    assert cache_settings() == (300, 24 * 3600)
    monkeypatch.setenv("SECRETS_MANAGER_TTL", "0")
    assert cache_settings() == (0, 24 * 3600)
    monkeypatch.setenv("SECRETS_MANAGER_TTL", "600")
    assert cache_settings() == (600, 24 * 3600)
    # Explicit settings take precedence, and max staleness is at least the
    # refresh interval
    assert cache_settings(
        SecretsManagerExtension(
            secrets_client=client,
            refresh_interval=timedelta(minutes=1),
            max_staleness=timedelta(seconds=30),
        )
    ) == (60, 60)

    for bad in ("-1", "1.5", "abc", " 5"):
        monkeypatch.setenv("SECRETS_MANAGER_TTL", bad)
        with pytest.raises(RuntimeError, match="invalid SECRETS_MANAGER_TTL"):
            cache_settings()

    with pytest.raises(RuntimeError, match="refresh_interval must not be negative"):
        cache_settings(
            SecretsManagerExtension(
                secrets_client=client, refresh_interval=timedelta(seconds=-1)
            )
        )


def test_default_fetcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    toml = """
        [profile.default.ext.aws_secrets_manager]
        api_key_id = "id"
        """
    ext = SecretsManagerExtension()
    assert isinstance(
        _plugin(_load(tmp_path, toml, extension=ext))._cache.fetcher, SDKFetcher
    )

    # Without boto3, the SDK fetcher is an error, but the Lambda extension works
    monkeypatch.setattr(_extension, "boto3_available", lambda: False)
    with pytest.raises(RuntimeError, match=r"requires boto3.*temporalio\[boto3\]"):
        _load(tmp_path, toml, extension=ext)
    monkeypatch.setenv("PARAMETERS_SECRETS_EXTENSION_HTTP_PORT", "2773")
    assert isinstance(
        _plugin(_load(tmp_path, toml, extension=ext))._cache.fetcher,
        LambdaExtensionFetcher,
    )


_plugins: list[_SecretsManagerPlugin] = []


@pytest.fixture(autouse=True)
async def cancel_refresh_tasks() -> AsyncIterator[None]:
    yield
    tasks = [t for p in _plugins for t in p._refresh_tasks]
    _plugins.clear()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _new_plugin(
    ids: Mapping[str, Any], client: _FakeSecretsClient, **kwargs: Any
) -> _SecretsManagerPlugin:
    plugin = SecretsManagerExtension(secrets_client=client, **kwargs).client_plugin(
        ClientConfigExtensionRequest(
            profile=ClientConfigProfile(ext={"aws_secrets_manager": ids}),
            env=None,
        )
    )
    assert isinstance(plugin, _SecretsManagerPlugin)
    _plugins.append(plugin)
    return plugin


async def _connect(
    plugin: _SecretsManagerPlugin, config: ConnectConfig
) -> tuple[ConnectConfig, MagicMock]:
    seen: list[ConnectConfig] = []
    service_client = MagicMock(spec=ServiceClient)

    async def next(config: ConnectConfig) -> ServiceClient:
        seen.append(config)
        return service_client

    assert await plugin.connect_service_client(config, next) is service_client
    return seen[0], service_client


async def test_connect_applies_secrets():
    cert_key = new_cert_key()
    ca = new_cert_key("ca")
    client = _FakeSecretsClient()
    client.set("api-key", "my-api-key", "v1")
    client.set("bundle", cert_key.cert + cert_key.key, "v1")
    client.set("ca", ca.cert, "v1")
    plugin = _new_plugin(
        {
            "api_key_id": "api-key",
            "tls": {
                "client_cert_id": "bundle",
                "client_key_id": "bundle",
                "server_ca_cert_id": "ca",
            },
        },
        client,
    )
    metrics = MetricsRecorder()

    orig = ConnectConfig(
        target_host="localhost:7233",
        tls=TLSConfig(domain="my-domain"),
        runtime=metrics.runtime,
    )
    config, _ = await _connect(plugin, orig)
    assert config.api_key == "my-api-key"
    assert config.tls == TLSConfig(
        domain="my-domain",
        client_cert=cert_key.cert,
        client_private_key=cert_key.key,
        server_root_ca_cert=ca.cert,
    )
    # The given config isn't modified
    assert orig.api_key is None
    assert orig.tls == TLSConfig(domain="my-domain")
    # Metrics go to the client's runtime
    assert sorted(
        a["secret_type"] for n, _, a in metrics.updates() if n == METRIC_CACHE_LOOKUP
    ) == ["api_key", "tls_client_cert", "tls_server_ca_cert"]

    # Connecting again uses the cache
    await _connect(plugin, ConnectConfig(target_host="localhost:7233"))
    assert sorted(client.calls) == ["api-key", "bundle", "ca"]


async def test_connect_api_key_leaves_tls_default():
    client = _FakeSecretsClient()
    client.set("api-key", "my-api-key", "v1")
    plugin = _new_plugin({"api_key_id": "api-key"}, client)
    config, _ = await _connect(plugin, ConnectConfig(target_host="localhost:7233"))
    assert config.api_key == "my-api-key"
    # Left unset so that it's enabled by default for the API key
    assert config.tls is None


async def test_connect_tls_secret_enables_tls():
    ca = new_cert_key("ca")
    client = _FakeSecretsClient()
    client.set("ca", ca.cert, "v1")
    plugin = _new_plugin({"tls": {"server_ca_cert_id": "ca"}}, client)
    for tls in (None, True):
        config, _ = await _connect(
            plugin, ConnectConfig(target_host="localhost:7233", tls=tls)
        )
        assert config.tls == TLSConfig(server_root_ca_cert=ca.cert)


@pytest.mark.parametrize(
    "ids,config,error",
    [
        (
            {"api_key_id": "api-key"},
            ConnectConfig(target_host="x", api_key="key"),
            "API key already set",
        ),
        (
            {"tls": {"server_ca_cert_id": "ca"}},
            ConnectConfig(target_host="x", tls=False),
            "TLS is disabled",
        ),
        (
            {"tls": {"client_cert_id": "bundle", "client_key_id": "bundle"}},
            ConnectConfig(target_host="x", tls=TLSConfig(client_cert=b"cert")),
            "client certificates already set",
        ),
        (
            {"tls": {"server_ca_cert_id": "ca"}},
            ConnectConfig(target_host="x", tls=TLSConfig(server_root_ca_cert=b"ca")),
            "server root CA cert already set",
        ),
    ],
)
async def test_connect_conflicts(
    ids: dict[str, Any], config: ConnectConfig, error: str
):
    client = _FakeSecretsClient()
    plugin = _new_plugin(ids, client)
    with pytest.raises(ValueError, match=error):
        await _connect(plugin, config)
    # Checked before fetching where possible
    if "api_key_id" in ids:
        assert not client.calls


async def test_connect_fails_on_fetch_failure():
    plugin = _new_plugin({"api_key_id": "missing"}, _FakeSecretsClient())
    with pytest.raises(RuntimeError, match='secret "missing"'):
        await _connect(plugin, ConnectConfig(target_host="x"))


async def test_api_key_refreshed_in_background(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_extension, "FAILED_REFRESH_RETRY_INTERVAL_SECONDS", 0.01)
    client = _FakeSecretsClient()
    client.set("api-key", "key-1", "v1")
    plugin = _new_plugin(
        {"api_key_id": "api-key"}, client, refresh_interval=timedelta(0)
    )
    config, service_client = await _connect(plugin, ConnectConfig(target_host="x"))
    assert config.api_key == "key-1"
    assert len(plugin._refresh_tasks) == 1
    task = next(iter(plugin._refresh_tasks))

    updates: list[str | None] = []
    service_client.update_api_key.side_effect = updates.append
    client.set("api-key", "key-2", "v2")
    await wait_until(lambda: bool(updates))
    assert updates == ["key-2"]

    # Fetch failures keep the current key
    entry = plugin._cache.api_key
    assert entry
    lookups = 0
    get = plugin._cache.get

    async def counting_get(*args: Any) -> Any:
        nonlocal lookups
        try:
            return await get(*args)
        finally:
            lookups += 1

    monkeypatch.setattr(plugin._cache, "get", counting_get)
    del client.secrets["api-key"]
    await wait_until(lambda: entry.failed_at is not None)
    # A lookup that completed after the failure, so its result was handled
    done = lookups
    await wait_until(lambda: lookups > done)
    assert updates == ["key-2"]
    assert not task.done()

    # Stops once the service client is gone
    del service_client
    gc.collect()
    await asyncio.wait_for(task, 5)
    assert not plugin._refresh_tasks


async def test_client_connect_uses_plugin(tmp_path: Path):
    """The plugin is applied by Client.connect with the loaded config."""
    client = _FakeSecretsClient()
    client.set("api-key", "my-api-key", "v1")
    config = _load(
        tmp_path,
        """
        [profile.default.ext.aws_secrets_manager]
        api_key_id = "api-key"
        """,
        extension=SecretsManagerExtension(secrets_client=client),
    )
    _plugin(config)
    temporal_client = await temporalio.client.Client.connect(**config, lazy=True)
    assert temporal_client.api_key == "my-api-key"
    assert temporal_client.service_client.config.tls is None
    assert client.calls == ["api-key"]
