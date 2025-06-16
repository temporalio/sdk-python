import os
import textwrap
from pathlib import Path

import pytest

from temporalio.envconfig import ClientConfig
from temporalio.service import TLSConfig

# A base TOML config with a default and a custom profile
TOML_CONFIG_BASE = textwrap.dedent(
    """
    [profile.default]
    address = "default-address"
    namespace = "default-namespace"

    [profile.custom]
    address = "custom-address"
    namespace = "custom-namespace"
    api_key = "custom-api-key"
    [profile.custom.tls]
    server_name = "custom-server-name"
    [profile.custom.grpc_meta]
    custom-header = "custom-value"
    """
)

# A TOML config with an unrecognized key for strict testing
TOML_CONFIG_STRICT_FAIL = textwrap.dedent(
    """
    [profile.default]
    address = "default-address"
    unrecognized = "should-fail"
    """
)

# Malformed TOML
TOML_CONFIG_MALFORMED = "this is not valid toml"

# A TOML config for testing detailed TLS options
TOML_CONFIG_TLS_DETAILED = textwrap.dedent(
    """
    [profile.tls_disabled]
    address = "localhost:1234"
    [profile.tls_disabled.tls]
    disabled = true
    server_name = "should-be-ignored"

    [profile.tls_with_certs]
    address = "localhost:5678"
    [profile.tls_with_certs.tls]
    server_name = "custom-server"
    server_ca_cert_data = "ca-pem-data"
    client_cert_data = "client-crt-data"
    client_key_data = "client-key-data"
    """
)


@pytest.fixture
def base_config_file(tmp_path: Path) -> Path:
    """Fixture to create a temporary config file with base content."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(TOML_CONFIG_BASE)
    return config_file


def test_load_profile_from_file_default(base_config_file: Path):
    """Test loading the default profile from a file."""
    profile = ClientConfig.load_profile_from_file(str(base_config_file))
    assert profile.address == "default-address"
    assert profile.namespace == "default-namespace"
    assert profile.tls is None
    assert "custom-header" not in profile.grpc_meta

    config = profile.to_connect_config()
    assert config.target_host == "default-address"
    assert config.rpc_metadata["namespace"] == "default-namespace"
    assert not config.tls
    assert not config.rpc_metadata or "custom-header" not in config.rpc_metadata


def test_load_profile_from_file_custom(base_config_file: Path):
    """Test loading a specific profile from a file."""
    profile = ClientConfig.load_profile_from_file(
        str(base_config_file), profile="custom"
    )
    assert profile.address == "custom-address"
    assert profile.namespace == "custom-namespace"
    assert profile.tls is not None
    assert profile.tls.server_name == "custom-server-name"
    assert profile.grpc_meta["custom-header"] == "custom-value"

    config = profile.to_connect_config()
    assert config.target_host == "custom-address"
    assert config.rpc_metadata["namespace"] == "custom-namespace"
    assert isinstance(config.tls, TLSConfig)
    assert config.tls.domain == "custom-server-name"
    assert config.rpc_metadata["custom-header"] == "custom-value"


def test_load_profile_from_data_default():
    """Test loading the default profile from raw TOML data."""
    profile = ClientConfig.load_profile_from_data(TOML_CONFIG_BASE)
    assert profile.address == "default-address"
    assert profile.namespace == "default-namespace"
    assert profile.tls is None

    config = profile.to_connect_config()
    assert config.target_host == "default-address"
    assert config.rpc_metadata["namespace"] == "default-namespace"
    assert not config.tls


def test_load_profile_from_data_custom():
    """Test loading a custom profile from raw TOML data."""
    profile = ClientConfig.load_profile_from_data(TOML_CONFIG_BASE, profile="custom")
    assert profile.address == "custom-address"
    assert profile.namespace == "custom-namespace"
    assert profile.tls is not None
    assert profile.tls.server_name == "custom-server-name"
    assert profile.grpc_meta["custom-header"] == "custom-value"

    config = profile.to_connect_config()
    assert config.target_host == "custom-address"
    assert config.rpc_metadata["namespace"] == "custom-namespace"
    assert isinstance(config.tls, TLSConfig)
    assert config.tls.domain == "custom-server-name"
    assert config.rpc_metadata["custom-header"] == "custom-value"


def test_load_profile_from_data_env_overrides():
    """Test that environment variables correctly override data settings."""
    env = {
        "TEMPORAL_ADDRESS": "env-address",
        "TEMPORAL_NAMESPACE": "env-namespace",
    }
    profile = ClientConfig.load_profile_from_data(
        TOML_CONFIG_BASE, profile="custom", env_vars=env
    )
    assert profile.address == "env-address"
    assert profile.namespace == "env-namespace"

    config = profile.to_connect_config()
    assert config.target_host == "env-address"
    assert config.rpc_metadata["namespace"] == "env-namespace"


def test_load_profile_env_overrides(base_config_file: Path):
    """Test that environment variables correctly override file settings."""
    env = {
        "TEMPORAL_ADDRESS": "env-address",
        "TEMPORAL_NAMESPACE": "env-namespace",
        "TEMPORAL_API_KEY": "env-api-key",
        "TEMPORAL_TLS_SERVER_NAME": "env-server-name",
    }
    profile = ClientConfig.load_profile_from_file(
        str(base_config_file), profile="custom", env_vars=env
    )
    assert profile.address == "env-address"
    assert profile.namespace == "env-namespace"
    assert profile.api_key == "env-api-key"
    assert profile.tls is not None
    assert profile.tls.server_name == "env-server-name"

    config = profile.to_connect_config()
    assert config.target_host == "env-address"
    assert config.rpc_metadata["namespace"] == "env-namespace"
    assert isinstance(config.tls, TLSConfig)
    assert config.api_key == "env-api-key"
    assert config.tls.domain == "env-server-name"


def test_load_profile_grpc_meta_env_overrides(base_config_file: Path):
    """Test gRPC metadata overrides from environment variables."""
    env = {
        # This should override the value in the file
        "TEMPORAL_GRPC_META_CUSTOM_HEADER": "env-value",
        # This should add a new header
        "TEMPORAL_GRPC_META_ANOTHER_HEADER": "another-value",
    }
    profile = ClientConfig.load_profile_from_file(
        str(base_config_file), profile="custom", env_vars=env
    )
    assert profile.grpc_meta["custom-header"] == "env-value"
    assert profile.grpc_meta["another-header"] == "another-value"

    config = profile.to_connect_config()
    assert config.rpc_metadata["custom-header"] == "env-value"
    assert config.rpc_metadata["another-header"] == "another-value"


def test_load_profile_disable_env(base_config_file: Path):
    """Test that `disable_env` prevents environment variable overrides."""
    env = {"TEMPORAL_ADDRESS": "env-address"}
    profile = ClientConfig.load_profile_from_file(
        str(base_config_file), env_vars=env, disable_env=True
    )
    assert profile.address == "default-address"

    config = profile.to_connect_config()
    assert config.target_host == "default-address"


def test_load_profile_disable_file(monkeypatch):
    """Test that `disable_file` loads configuration only from environment."""
    monkeypatch.setattr("pathlib.Path.exists", lambda _: False)
    env = {"TEMPORAL_ADDRESS": "env-address"}
    profile = ClientConfig.load_profile(disable_file=True, env_vars=env)
    assert profile.address == "env-address"

    config = profile.to_connect_config()
    assert config.target_host == "env-address"


def test_load_profile_api_key_enables_tls(tmp_path: Path):
    """Test that the presence of an API key enables TLS by default."""
    config_toml = "[profile.default]\naddress = 'some-host:1234'\napi_key = 'my-key'"
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_toml)
    profile = ClientConfig.load_profile_from_file(str(config_file))
    assert profile.api_key == "my-key"
    assert profile.tls is not None

    config = profile.to_connect_config()
    assert config.tls
    assert config.api_key == "my-key"


def test_load_profile_not_found(base_config_file: Path):
    """Test that requesting a non-existent profile raises an error."""
    with pytest.raises(RuntimeError, match="Profile 'nonexistent' not found"):
        ClientConfig.load_profile_from_file(
            str(base_config_file), profile="nonexistent"
        )


def test_load_profiles_from_file_all(base_config_file: Path):
    """Test loading all profiles from a file."""
    client_config = ClientConfig.load_profiles_from_file(str(base_config_file))
    assert len(client_config.profiles) == 2
    assert "default" in client_config.profiles
    assert "custom" in client_config.profiles
    # Check that we can convert to a connect config
    connect_config = client_config.profiles["default"].to_connect_config()
    assert connect_config.target_host == "default-address"


def test_load_profiles_from_data_all():
    """Test loading all profiles from raw data."""
    client_config = ClientConfig.load_profiles_from_data(TOML_CONFIG_BASE)
    assert len(client_config.profiles) == 2
    connect_config = client_config.profiles["custom"].to_connect_config()
    assert connect_config.target_host == "custom-address"


def test_load_profiles_no_env_override(tmp_path: Path, monkeypatch):
    """Confirm that load_profiles does not apply env overrides."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(TOML_CONFIG_BASE)
    env = {
        "TEMPORAL_CONFIG_FILE": str(config_file),
        "TEMPORAL_ADDRESS": "env-address",  # This should be ignored
    }
    client_config = ClientConfig.load_profiles(env_vars=env)
    connect_config = client_config.profiles["default"].to_connect_config()
    assert connect_config.target_host == "default-address"


def test_load_profiles_no_config_file(monkeypatch):
    """Test that load_profiles works when no config file is found."""
    monkeypatch.setattr("pathlib.Path.exists", lambda _: False)
    monkeypatch.setattr(os, "environ", {})
    client_config = ClientConfig.load_profiles(env_vars={})
    assert not client_config.profiles


def test_load_profiles_discovery(tmp_path: Path, monkeypatch):
    """Test file discovery via environment variables."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(TOML_CONFIG_BASE)
    env = {"TEMPORAL_CONFIG_FILE": str(config_file)}
    client_config = ClientConfig.load_profiles(env_vars=env)
    assert "default" in client_config.profiles


def test_load_profiles_disable_file():
    """Test load_profiles with file loading disabled."""
    # With no env vars, should be empty
    client_config = ClientConfig.load_profiles(disable_file=True, env_vars={})
    assert not client_config.profiles


def test_load_profiles_strict_mode_fail(tmp_path: Path):
    """Test that strict mode fails on unrecognized keys."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(TOML_CONFIG_STRICT_FAIL)
    with pytest.raises(RuntimeError, match="unknown field `unrecognized`"):
        ClientConfig.load_profiles_from_file(str(config_file), config_file_strict=True)


def test_load_profile_strict_mode_fail(tmp_path: Path):
    """Test that strict mode fails on unrecognized keys for load_profile."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(TOML_CONFIG_STRICT_FAIL)
    with pytest.raises(RuntimeError, match="unknown field `unrecognized`"):
        ClientConfig.load_profile_from_file(str(config_file), config_file_strict=True)


def test_load_profiles_from_data_malformed():
    """Test that loading malformed TOML data raises an error."""
    with pytest.raises(RuntimeError, match="TOML parse error"):
        ClientConfig.load_profiles_from_data(TOML_CONFIG_MALFORMED)


def test_load_profile_tls_options():
    """Test parsing of detailed TLS options from data."""
    # Test with TLS disabled
    profile_disabled = ClientConfig.load_profile_from_data(
        TOML_CONFIG_TLS_DETAILED, profile="tls_disabled"
    )
    assert profile_disabled.tls is not None
    assert profile_disabled.tls.disabled is True

    config_disabled = profile_disabled.to_connect_config()
    assert not config_disabled.tls

    # Test with TLS certs
    profile_certs = ClientConfig.load_profile_from_data(
        TOML_CONFIG_TLS_DETAILED, profile="tls_with_certs"
    )
    assert profile_certs.tls is not None
    assert profile_certs.tls.server_name == "custom-server"
    assert profile_certs.tls.server_root_ca_cert is not None
    assert profile_certs.tls.server_root_ca_cert == b"ca-pem-data"
    assert profile_certs.tls.client_cert is not None
    assert profile_certs.tls.client_cert == b"client-crt-data"
    assert profile_certs.tls.client_private_key is not None
    assert profile_certs.tls.client_private_key == b"client-key-data"

    config_certs = profile_certs.to_connect_config()
    assert isinstance(config_certs.tls, TLSConfig)
    assert config_certs.tls.domain == "custom-server"
    assert config_certs.tls.server_root_ca_cert == b"ca-pem-data"
    assert config_certs.tls.client_cert == b"client-crt-data"
    assert config_certs.tls.client_private_key == b"client-key-data"


def test_load_profile_tls_from_paths(tmp_path: Path):
    """Test parsing of TLS options from file paths."""
    # Create dummy cert files
    (tmp_path / "ca.pem").write_text("ca-pem-data")
    (tmp_path / "client.crt").write_text("client-crt-data")
    (tmp_path / "client.key").write_text("client-key-data")

    ca_pem_path = (tmp_path / "ca.pem").as_posix()
    client_crt_path = (tmp_path / "client.crt").as_posix()
    client_key_path = (tmp_path / "client.key").as_posix()

    toml_config = textwrap.dedent(
        f"""
        [profile.default]
        address = "localhost:5678"
        [profile.default.tls]
        server_name = "custom-server"
        server_ca_cert_path = "{ca_pem_path}"
        client_cert_path = "{client_crt_path}"
        client_key_path = "{client_key_path}"
        """
    )

    profile = ClientConfig.load_profile_from_data(toml_config)
    assert profile.tls is not None
    assert profile.tls.server_name == "custom-server"
    assert profile.tls.server_root_ca_cert is not None
    assert profile.tls.server_root_ca_cert == ca_pem_path
    assert profile.tls.client_cert is not None
    assert profile.tls.client_cert == client_crt_path
    assert profile.tls.client_private_key is not None
    assert profile.tls.client_private_key == client_key_path

    config = profile.to_connect_config()
    assert isinstance(config.tls, TLSConfig)
    assert config.tls.domain == "custom-server"
    assert config.tls.server_root_ca_cert == b"ca-pem-data"
    assert config.tls.client_cert == b"client-crt-data"
    assert config.tls.client_private_key == b"client-key-data"


def test_load_profile_conflicting_cert_source_fails():
    """Test that providing both path and data for a cert fails."""
    toml_config = textwrap.dedent(
        """
        [profile.default]
        address = "localhost:5678"
        [profile.default.tls]
        client_cert_path = "/path/to/cert"
        client_cert_data = "cert-data"
        """
    )
    with pytest.raises(
        RuntimeError, match="Cannot specify both client_cert_path and client_cert_data"
    ):
        ClientConfig.load_profile_from_data(toml_config)


def test_disables_raise_error():
    """Test that providing both disable_file and disable_env raises an error."""
    with pytest.raises(ValueError, match="Cannot disable both"):
        ClientConfig.load_profile(disable_file=True, disable_env=True)
