import dataclasses
import math
import os
import textwrap
from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import temporalio.client
from temporalio.envconfig import (
    ClientConfig,
    ClientConfigExtension,
    ClientConfigExtensionRequest,
    ClientConfigProfile,
)
from temporalio.service import ConnectConfig, ServiceClient


class _TestPlugin(temporalio.client.Plugin):
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name

    def configure_client(
        self, config: temporalio.client.ClientConfig
    ) -> temporalio.client.ClientConfig:
        return config

    async def connect_service_client(
        self,
        config: ConnectConfig,
        next: Callable[[ConnectConfig], Awaitable[ServiceClient]],
    ) -> ServiceClient:
        return await next(config)


class _TestExtension(ClientConfigExtension):
    def __init__(
        self,
        name: str,
        client_plugin: Callable[
            [ClientConfigExtensionRequest], temporalio.client.Plugin | None
        ],
    ) -> None:
        self._name = name
        self._client_plugin = client_plugin

    def name(self) -> str:
        return self._name

    def client_plugin(
        self, req: ClientConfigExtensionRequest
    ) -> temporalio.client.Plugin | None:
        return self._client_plugin(req)


def _write_config(tmp_path: Path, content: str) -> str:
    config_file = tmp_path / "config.toml"
    config_file.write_text(textwrap.dedent(content))
    return str(config_file)


TOML_CONFIG_EXT = textwrap.dedent(
    """
    [profile.default]
    address = "default-address"
    [profile.default.ext.first]
    value = "first-value"
    nested = { list = [1, 2], flag = true }
    [profile.default.ext.second]
    [profile.default.ext.second.deeper]
    value = 5

    [profile.other]
    address = "other-address"
    """
)


@pytest.mark.parametrize("strict", [False, True])
def test_load_profile_with_ext(strict: bool):
    profile = ClientConfigProfile.load(
        config_source=TOML_CONFIG_EXT,
        config_file_strict=strict,
        override_env_vars={},
    )
    assert profile.address == "default-address"
    assert profile.ext == {
        "first": {"value": "first-value", "nested": {"list": [1, 2], "flag": True}},
        "second": {"deeper": {"value": 5}},
    }

    config = ClientConfig.load(config_source=TOML_CONFIG_EXT, config_file_strict=strict)
    assert config.profiles["default"].ext == profile.ext
    assert config.profiles["other"].ext == {}


def test_load_profile_ext_value_conversion():
    profile = ClientConfigProfile.load(
        config_source=textwrap.dedent(
            """
            [profile.default.ext.my_ext]
            str = "value"
            int = 5
            float = 1.5
            nan = nan
            inf = -inf
            bool = true
            datetime = 1979-05-27T07:32:00Z
            local_date = 1979-05-27
            local_time = 07:32:00
            array = [1, "two", 1979-05-27]
            nested = { key = "nested_value" }
            """
        ),
        disable_env=True,
    )
    ext = dict(profile.ext["my_ext"])
    assert math.isnan(ext.pop("nan"))
    assert ext == {
        "str": "value",
        "int": 5,
        "float": 1.5,
        "inf": -math.inf,
        "bool": True,
        "datetime": "1979-05-27T07:32:00Z",
        "local_date": "1979-05-27",
        "local_time": "07:32:00",
        "array": [1, "two", "1979-05-27"],
        "nested": {"key": "nested_value"},
    }


def test_strict_mode_still_rejects_unknown_non_ext_keys():
    with pytest.raises(RuntimeError, match="unrecognized_field"):
        ClientConfigProfile.load(
            config_source=textwrap.dedent(
                """
                [profile.default]
                unrecognized_field = "should-fail"
                [profile.default.ext.first]
                value = "first-value"
                """
            ),
            config_file_strict=True,
        )


def test_ext_not_modified_by_env():
    profile = ClientConfigProfile.load(
        config_source='[profile.default.ext.foo]\nsome_field = "value"',
        override_env_vars={"TEMPORAL_EXT_FOO_SOME_FIELD": "env-value"},
    )
    assert profile.ext == {"foo": {"some_field": "value"}}


def test_profile_ext_to_from_dict():
    profile = ClientConfigProfile(
        address="some-address", ext={"foo": {"some_field": "value"}}
    )
    profile_dict = profile.to_dict()
    assert profile_dict == {
        "address": "some-address",
        "ext": {"foo": {"some_field": "value"}},
    }
    assert ClientConfigProfile.from_dict(profile_dict) == profile


def test_load_client_connect_config_calls_extensions_in_order(tmp_path: Path):
    env = {"TEMPORAL_ADDRESS": "env-address", "TEMPORAL_EXT_FIRST_VALUE": "env-value"}
    calls: list[str] = []

    def first_plugin(req: ClientConfigExtensionRequest) -> temporalio.client.Plugin:
        calls.append("first")
        # Env vars have already been applied to the profile
        assert req.profile.address == "env-address"
        assert req.profile.ext.get("first") == {"value": "file-value"}
        assert req.env is env
        assert req.env is not None
        return _TestPlugin(f"first-{req.env['TEMPORAL_EXT_FIRST_VALUE']}")

    def noop_plugin(req: ClientConfigExtensionRequest) -> None:
        calls.append("noop")
        assert "noop" not in req.profile.ext
        return None

    def second_plugin(_: ClientConfigExtensionRequest) -> temporalio.client.Plugin:
        calls.append("second")
        return _TestPlugin("second")

    config = ClientConfig.load_client_connect_config(
        config_file=_write_config(
            tmp_path,
            """
            [profile.default]
            address = "file-address"
            [profile.default.ext.first]
            value = "file-value"
            """,
        ),
        config_file_strict=True,
        override_env_vars=env,
        extensions=[
            _TestExtension("second", second_plugin),
            _TestExtension("first", first_plugin),
            _TestExtension("noop", noop_plugin),
        ],
    )
    # Extension order is respected
    assert calls == ["second", "first", "noop"]
    assert [p.name() for p in config.get("plugins", [])] == [
        "second",
        "first-env-value",
    ]
    assert config.get("target_host") == "env-address"


def test_load_client_connect_config_no_plugins_without_extensions(tmp_path: Path):
    config_file = _write_config(tmp_path, TOML_CONFIG_EXT)
    config = ClientConfig.load_client_connect_config(
        config_file=config_file, override_env_vars={}
    )
    assert "plugins" not in config
    config = ClientConfig.load_client_connect_config(
        config_file=config_file,
        override_env_vars={},
        extensions=[_TestExtension("first", lambda _: None)],
    )
    assert "plugins" not in config


def test_load_client_connect_config_passes_env_to_extensions(tmp_path: Path):
    config_file = _write_config(tmp_path, "[profile.default]")
    got_envs: list[object] = []

    def record_env(req: ClientConfigExtensionRequest) -> None:
        got_envs.append(req.env)
        return None

    extensions = [_TestExtension("ext", record_env)]
    env = {"TEMPORAL_EXT_EXT_VALUE": "value"}

    # Defaults to process env
    ClientConfig.load_client_connect_config(
        config_file=config_file, extensions=extensions
    )
    # Uses override env when given
    ClientConfig.load_client_connect_config(
        config_file=config_file, override_env_vars=env, extensions=extensions
    )
    # None when env is disabled
    ClientConfig.load_client_connect_config(
        config_file=config_file,
        disable_env=True,
        override_env_vars=env,
        extensions=extensions,
    )

    assert len(got_envs) == 3
    assert got_envs[0] is os.environ
    assert got_envs[1] is env
    assert got_envs[2] is None


@pytest.mark.parametrize(
    "names,expected_message",
    [
        (["a", "b", "a"], 'duplicate client config extension name "a"'),
        (["a", ""], "client config extension at index 1 has empty name"),
        ([None], "client config extension at index 0 is None"),
    ],
)
def test_load_client_connect_config_rejects_invalid_extensions(
    tmp_path: Path, names: list[str | None], expected_message: str
):
    def fail(_: ClientConfigExtensionRequest) -> None:
        pytest.fail("extension should not be called")

    extensions = [
        None if name is None else _TestExtension(name, fail) for name in names
    ]
    with pytest.raises(ValueError) as err:
        ClientConfig.load_client_connect_config(
            config_file=_write_config(tmp_path, "[profile.default]"),
            override_env_vars={},
            extensions=extensions,  # type: ignore[arg-type]
        )
    assert str(err.value) == expected_message


def test_load_client_connect_config_wraps_extension_errors(tmp_path: Path):
    ext_err = ValueError("bad config")

    def raise_err(_: ClientConfigExtensionRequest) -> None:
        raise ext_err

    with pytest.raises(RuntimeError) as err:
        ClientConfig.load_client_connect_config(
            config_file=_write_config(tmp_path, "[profile.default]"),
            override_env_vars={},
            extensions=[_TestExtension("bad", raise_err)],
        )
    assert str(err.value) == 'client config extension "bad": bad config'
    assert err.value.__cause__ is ext_err


class ApiKeyFileExtension(ClientConfigExtension):
    """Example extension that reads an API key from a file, configured like:

    .. code-block:: toml

        [profile.default.ext.api_key_file]
        path = "/path/to/api-key"

    or with the ``TEMPORAL_EXT_API_KEY_FILE_PATH`` env var, which takes
    precedence.
    """

    def name(self) -> str:
        return "api_key_file"

    def client_plugin(
        self, req: ClientConfigExtensionRequest
    ) -> temporalio.client.Plugin | None:
        key_path = req.profile.ext.get(self.name(), {}).get("path")
        if key_path is not None and not isinstance(key_path, str):
            raise TypeError(f"path must be a string, got {type(key_path).__name__}")
        if req.env is not None:
            key_path = req.env.get("TEMPORAL_EXT_API_KEY_FILE_PATH", key_path)
        if key_path is None:
            return None
        return _ApiKeyPlugin(Path(key_path).read_text().strip())


class _ApiKeyPlugin(temporalio.client.Plugin):
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def configure_client(
        self, config: temporalio.client.ClientConfig
    ) -> temporalio.client.ClientConfig:
        return config

    async def connect_service_client(
        self,
        config: ConnectConfig,
        next: Callable[[ConnectConfig], Awaitable[ServiceClient]],
    ) -> ServiceClient:
        return await next(
            dataclasses.replace(
                config,
                api_key=self._api_key,
                tls=True if config.tls is None else config.tls,
            )
        )


async def test_example_extension(tmp_path: Path):
    key_file = tmp_path / "api-key"
    key_file.write_text("my-api-key\n")
    config = ClientConfig.load_client_connect_config(
        config_file=_write_config(
            tmp_path,
            f"""
            [profile.default]
            address = "my-address"
            [profile.default.ext.api_key_file]
            path = {str(key_file)!r}
            """,
        ),
        config_file_strict=True,
        override_env_vars={},
        extensions=[ApiKeyFileExtension()],
    )
    plugins = config.get("plugins", [])
    assert len(plugins) == 1
    next = AsyncMock()
    await plugins[0].connect_service_client(
        ConnectConfig(target_host="my-address"), next
    )
    connect_config = next.call_args.args[0]
    assert connect_config.target_host == "my-address"
    assert connect_config.api_key == "my-api-key"
    assert connect_config.tls is True

    # Nothing configured
    unconfigured = ClientConfig.load_client_connect_config(
        config_file=_write_config(tmp_path, "[profile.default]"),
        override_env_vars={},
        extensions=[ApiKeyFileExtension()],
    )
    assert "plugins" not in unconfigured

    # Invalid config
    with pytest.raises(RuntimeError) as err:
        ClientConfig.load_client_connect_config(
            config_file=_write_config(
                tmp_path, "[profile.default.ext.api_key_file]\npath = 5"
            ),
            override_env_vars={},
            extensions=[ApiKeyFileExtension()],
        )
    assert (
        str(err.value)
        == 'client config extension "api_key_file": path must be a string, got int'
    )
