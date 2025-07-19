import os
import textwrap
from pathlib import Path

import pytest

from temporalio.client import Client
from temporalio.envconfig import ClientConfig, ClientConfigProfile, ClientConfigTLS
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
    profile = ClientConfigProfile.load(config_source=base_config_file)
    assert profile.address == "default-address"
    assert profile.namespace == "default-namespace"
    assert profile.tls is None
    assert "custom-header" not in profile.grpc_meta

    config = profile.to_client_connect_config()
    assert config.get("target_host") == "default-address"
    assert "tls" not in config
    rpc_meta = config.get("rpc_metadata")
    assert not rpc_meta or "custom-header" not in rpc_meta


def test_load_profile_from_file_custom(base_config_file: Path):
    """Test loading a specific profile from a file."""
    profile = ClientConfigProfile.load(config_source=base_config_file, profile="custom")
    assert profile.address == "custom-address"
    assert profile.namespace == "custom-namespace"
    assert profile.tls is not None
    assert profile.tls.server_name == "custom-server-name"
    assert profile.grpc_meta["custom-header"] == "custom-value"

    config = profile.to_client_connect_config()
    assert config.get("target_host") == "custom-address"
    tls_config = config.get("tls")
    assert isinstance(tls_config, TLSConfig)
    assert tls_config.domain == "custom-server-name"
    rpc_metadata = config.get("rpc_metadata")
    assert rpc_metadata
    assert rpc_metadata["custom-header"] == "custom-value"


def test_load_profile_from_data_default():
    """Test loading the default profile from raw TOML data."""
    profile = ClientConfigProfile.load(config_source=TOML_CONFIG_BASE)
    assert profile.address == "default-address"
    assert profile.namespace == "default-namespace"
    assert profile.tls is None

    config = profile.to_client_connect_config()
    assert config.get("target_host") == "default-address"
    assert "tls" not in config


def test_load_profile_from_data_custom():
    """Test loading a custom profile from raw TOML data."""
    profile = ClientConfigProfile.load(config_source=TOML_CONFIG_BASE, profile="custom")
    assert profile.address == "custom-address"
    assert profile.namespace == "custom-namespace"
    assert profile.tls is not None
    assert profile.tls.server_name == "custom-server-name"
    assert profile.grpc_meta["custom-header"] == "custom-value"

    config = profile.to_client_connect_config()
    assert config.get("target_host") == "custom-address"
    tls_config = config.get("tls")
    assert isinstance(tls_config, TLSConfig)
    assert tls_config.domain == "custom-server-name"
    rpc_metadata = config.get("rpc_metadata")
    assert rpc_metadata
    assert rpc_metadata["custom-header"] == "custom-value"


def test_load_profile_from_data_env_overrides():
    """Test that environment variables correctly override data settings."""
    env = {
        "TEMPORAL_ADDRESS": "env-address",
        "TEMPORAL_NAMESPACE": "env-namespace",
    }
    profile = ClientConfigProfile.load(
        config_source=TOML_CONFIG_BASE, profile="custom", override_env_vars=env
    )
    assert profile.address == "env-address"
    assert profile.namespace == "env-namespace"

    config = profile.to_client_connect_config()
    assert config.get("target_host") == "env-address"


def test_load_profile_env_overrides(base_config_file: Path):
    """Test that environment variables correctly override file settings."""
    env = {
        "TEMPORAL_ADDRESS": "env-address",
        "TEMPORAL_NAMESPACE": "env-namespace",
        "TEMPORAL_API_KEY": "env-api-key",
        "TEMPORAL_TLS_SERVER_NAME": "env-server-name",
    }
    profile = ClientConfigProfile.load(
        config_source=base_config_file, profile="custom", override_env_vars=env
    )
    assert profile.address == "env-address"
    assert profile.namespace == "env-namespace"
    assert profile.api_key == "env-api-key"
    assert profile.tls is not None
    assert profile.tls.server_name == "env-server-name"

    config = profile.to_client_connect_config()
    assert config.get("target_host") == "env-address"
    assert config.get("api_key") == "env-api-key"
    tls_config = config.get("tls")
    assert isinstance(tls_config, TLSConfig)
    assert tls_config.domain == "env-server-name"


def test_load_profile_grpc_meta_env_overrides(base_config_file: Path):
    """Test gRPC metadata overrides from environment variables."""
    env = {
        # This should override the value in the file
        "TEMPORAL_GRPC_META_CUSTOM_HEADER": "env-value",
        # This should add a new header
        "TEMPORAL_GRPC_META_ANOTHER_HEADER": "another-value",
    }
    profile = ClientConfigProfile.load(
        config_source=base_config_file, profile="custom", override_env_vars=env
    )
    assert profile.grpc_meta["custom-header"] == "env-value"
    assert profile.grpc_meta["another-header"] == "another-value"

    config = profile.to_client_connect_config()
    rpc_metadata = config.get("rpc_metadata")
    assert rpc_metadata
    assert rpc_metadata["custom-header"] == "env-value"
    assert rpc_metadata["another-header"] == "another-value"


def test_load_profile_disable_env(base_config_file: Path):
    """Test that `disable_env` prevents environment variable overrides."""
    env = {"TEMPORAL_ADDRESS": "env-address"}
    profile = ClientConfigProfile.load(
        config_source=base_config_file, override_env_vars=env, disable_env=True
    )
    assert profile.address == "default-address"

    config = profile.to_client_connect_config()
    assert config.get("target_host") == "default-address"


def test_load_profile_disable_file(monkeypatch):  # type: ignore[reportMissingParameterType]
    """Test that `disable_file` loads configuration only from environment."""
    monkeypatch.setattr("pathlib.Path.exists", lambda _: False)
    env = {"TEMPORAL_ADDRESS": "env-address"}
    profile = ClientConfigProfile.load(disable_file=True, override_env_vars=env)
    assert profile.address == "env-address"

    config = profile.to_client_connect_config()
    assert config.get("target_host") == "env-address"


def test_load_profile_api_key_enables_tls(tmp_path: Path):
    """Test that the presence of an API key enables TLS by default."""
    config_toml = "[profile.default]\naddress = 'some-host:1234'\napi_key = 'my-key'"
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_toml)
    profile = ClientConfigProfile.load(config_source=config_file)
    assert profile.api_key == "my-key"
    assert profile.tls is not None

    config = profile.to_client_connect_config()
    assert config.get("tls")
    assert config.get("api_key") == "my-key"


def test_load_profile_not_found(base_config_file: Path):
    """Test that requesting a non-existent profile raises an error."""
    with pytest.raises(RuntimeError, match="Profile 'nonexistent' not found"):
        ClientConfigProfile.load(config_source=base_config_file, profile="nonexistent")


def test_load_profiles_from_file_all(base_config_file: Path):
    """Test loading all profiles from a file."""
    client_config = ClientConfig.load(config_source=base_config_file)
    assert len(client_config.profiles) == 2
    assert "default" in client_config.profiles
    assert "custom" in client_config.profiles
    # Check that we can convert to a connect config
    connect_config = client_config.profiles["default"].to_client_connect_config()
    assert connect_config.get("target_host") == "default-address"


def test_load_profiles_from_data_all():
    """Test loading all profiles from raw data."""
    client_config = ClientConfig.load(config_source=TOML_CONFIG_BASE)
    assert len(client_config.profiles) == 2
    connect_config = client_config.profiles["custom"].to_client_connect_config()
    assert connect_config.get("target_host") == "custom-address"


def test_load_profiles_no_env_override(tmp_path: Path, monkeypatch):
    """Confirm that load_profiles does not apply env overrides."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(TOML_CONFIG_BASE)
    env = {
        "TEMPORAL_CONFIG_FILE": str(config_file),
        "TEMPORAL_ADDRESS": "env-address",  # This should be ignored
    }
    client_config = ClientConfig.load(override_env_vars=env)
    connect_config = client_config.profiles["default"].to_client_connect_config()
    assert connect_config.get("target_host") == "default-address"


def test_load_profiles_no_config_file(monkeypatch):  # type: ignore[reportMissingParameterType]
    """Test that load_profiles works when no config file is found."""
    monkeypatch.setattr("pathlib.Path.exists", lambda _: False)
    monkeypatch.setattr(os, "environ", {})
    client_config = ClientConfig.load(override_env_vars={})
    assert not client_config.profiles


def test_load_profiles_discovery(tmp_path: Path, monkeypatch):  # type: ignore[reportMissingParameterType]
    """Test file discovery via environment variables."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(TOML_CONFIG_BASE)
    env = {"TEMPORAL_CONFIG_FILE": str(config_file)}
    client_config = ClientConfig.load(override_env_vars=env)
    assert "default" in client_config.profiles


def test_load_profiles_disable_file():
    """Test load_profiles with file loading disabled."""
    # With no env vars, should be empty
    client_config = ClientConfig.load(disable_file=True, override_env_vars={})
    assert not client_config.profiles


def test_load_profiles_strict_mode_fail(tmp_path: Path):
    """Test that strict mode fails on unrecognized keys."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(TOML_CONFIG_STRICT_FAIL)
    with pytest.raises(RuntimeError, match="unknown field `unrecognized`"):
        ClientConfig.load(config_source=config_file, config_file_strict=True)


def test_load_profile_strict_mode_fail(tmp_path: Path):
    """Test that strict mode fails on unrecognized keys for load_profile."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(TOML_CONFIG_STRICT_FAIL)
    with pytest.raises(RuntimeError, match="unknown field `unrecognized`"):
        ClientConfigProfile.load(config_source=config_file, config_file_strict=True)


def test_load_profiles_from_data_malformed():
    """Test that loading malformed TOML data raises an error."""
    with pytest.raises(RuntimeError, match="TOML parse error"):
        ClientConfig.load(config_source=TOML_CONFIG_MALFORMED)


def test_load_profile_tls_options():
    """Test parsing of detailed TLS options from data."""
    # Test with TLS disabled
    profile_disabled = ClientConfigProfile.load(
        config_source=TOML_CONFIG_TLS_DETAILED, profile="tls_disabled"
    )
    assert profile_disabled.tls is not None
    assert profile_disabled.tls.disabled is True

    config_disabled = profile_disabled.to_client_connect_config()
    assert not config_disabled.get("tls")

    # Test with TLS certs
    profile_certs = ClientConfigProfile.load(
        config_source=TOML_CONFIG_TLS_DETAILED, profile="tls_with_certs"
    )
    assert profile_certs.tls is not None
    assert profile_certs.tls.server_name == "custom-server"
    assert profile_certs.tls.server_root_ca_cert is not None
    assert profile_certs.tls.server_root_ca_cert == b"ca-pem-data"
    assert profile_certs.tls.client_cert is not None
    assert profile_certs.tls.client_cert == b"client-crt-data"
    assert profile_certs.tls.client_private_key is not None
    assert profile_certs.tls.client_private_key == b"client-key-data"

    config_certs = profile_certs.to_client_connect_config()
    tls_config_certs = config_certs.get("tls")
    assert isinstance(tls_config_certs, TLSConfig)
    assert tls_config_certs.domain == "custom-server"
    assert tls_config_certs.server_root_ca_cert == b"ca-pem-data"
    assert tls_config_certs.client_cert == b"client-crt-data"
    assert tls_config_certs.client_private_key == b"client-key-data"


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

    profile = ClientConfigProfile.load(config_source=toml_config)
    assert profile.tls is not None
    assert profile.tls.server_name == "custom-server"
    assert profile.tls.server_root_ca_cert is not None
    assert profile.tls.server_root_ca_cert == Path(ca_pem_path)
    assert profile.tls.client_cert is not None
    assert profile.tls.client_cert == Path(client_crt_path)
    assert profile.tls.client_private_key is not None
    assert profile.tls.client_private_key == Path(client_key_path)

    config = profile.to_client_connect_config()
    tls_config = config.get("tls")
    assert isinstance(tls_config, TLSConfig)
    assert tls_config.domain == "custom-server"
    assert tls_config.server_root_ca_cert == b"ca-pem-data"
    assert tls_config.client_cert == b"client-crt-data"
    assert tls_config.client_private_key == b"client-key-data"


def test_read_source_from_string_content():
    """Test that _read_source correctly encodes string content."""
    # Check the behavior of providing a string as a data
    # source, ensuring it's treated as content and encoded to bytes.
    # Note that string content can only be provided programmatically, as
    # the TOML parser in core currently only supports reading file paths
    # and file data as bytes in the config file.
    profile = ClientConfigProfile(
        address="localhost:1234",
        tls=ClientConfigTLS(client_cert="string-as-cert-content"),
    )
    config = profile.to_client_connect_config()
    tls_config = config.get("tls")
    assert isinstance(tls_config, TLSConfig)
    assert tls_config.client_cert == b"string-as-cert-content"


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
        ClientConfigProfile.load(config_source=toml_config)


async def test_load_client_connect_config(client: Client, tmp_path: Path):
    """Test the load_client_connect_config for various scenarios."""
    # Get connection details from the fixture client
    target_host = client.service_client.config.target_host
    namespace = client.namespace

    # Create a TOML file with profiles pointing to the test server
    config_content = f"""
[profile.default]
address = "{target_host}"
namespace = "{namespace}"

[profile.custom]
address = "{target_host}"
namespace = "custom-namespace"
[profile.custom.grpc_meta]
custom-header = "custom-value"
    """
    config_file = tmp_path / "temporal.toml"
    config_file.write_text(config_content)

    # Test with explicit file path, default profile
    config = ClientConfig.load_client_connect_config(config_file=str(config_file))
    assert config.get("target_host") == target_host
    assert config.get("namespace") == namespace
    new_client = await Client.connect(**config)  # type: ignore
    assert new_client.service_client.config.target_host == target_host
    assert new_client.namespace == namespace

    # Test with explicit file path, custom profile
    config = ClientConfig.load_client_connect_config(
        config_file=str(config_file), profile="custom"
    )
    assert config.get("target_host") == target_host
    assert config.get("namespace") == "custom-namespace"
    rpc_metadata = config.get("rpc_metadata")
    assert rpc_metadata
    assert "custom-header" in rpc_metadata
    new_client = await Client.connect(**config)  # type: ignore
    assert new_client.service_client.config.target_host == target_host
    assert new_client.namespace == "custom-namespace"
    assert (
        new_client.service_client.config.rpc_metadata["custom-header"] == "custom-value"
    )

    # Test with env overrides
    env = {"TEMPORAL_NAMESPACE": "env-namespace-override"}
    config = ClientConfig.load_client_connect_config(
        config_file=str(config_file), override_env_vars=env
    )
    assert config.get("target_host") == target_host
    assert config.get("namespace") == "env-namespace-override"
    new_client = await Client.connect(**config)  # type: ignore
    assert new_client.namespace == "env-namespace-override"

    # Test with env overrides disabled
    config = ClientConfig.load_client_connect_config(
        config_file=str(config_file),
        override_env_vars={"TEMPORAL_NAMESPACE": "ignored"},
        disable_env=True,
    )
    assert config.get("target_host") == target_host
    assert config.get("namespace") == namespace
    new_client = await Client.connect(**config)  # type: ignore
    assert new_client.namespace == namespace

    # Test with file loading disabled (so only env is used)
    env = {
        "TEMPORAL_ADDRESS": target_host,
        "TEMPORAL_NAMESPACE": "env-only-namespace",
    }
    config = ClientConfig.load_client_connect_config(
        disable_file=True, override_env_vars=env
    )
    assert config.get("target_host") == target_host
    assert config.get("namespace") == "env-only-namespace"
    new_client = await Client.connect(**config)  # type: ignore
    assert new_client.service_client.config.target_host == target_host
    assert new_client.namespace == "env-only-namespace"


def test_disables_raise_error():
    """Test that providing both disable_file and disable_env raises an error."""
    with pytest.raises(RuntimeError, match="Cannot disable both"):
        ClientConfigProfile.load(disable_file=True, disable_env=True)


def test_client_config_profile_to_from_dict():
    """Test round-trip ClientConfigProfile to and from a dictionary."""
    # Profile with all fields
    profile = ClientConfigProfile(
        address="some-address",
        namespace="some-namespace",
        api_key="some-api-key",
        tls=ClientConfigTLS(
            disabled=False,
            server_name="some-server-name",
            server_root_ca_cert=b"ca-cert-data",
            client_cert=Path("/path/to/client.crt"),
            client_private_key="client-key-data",
        ),
        grpc_meta={"some-header": "some-value"},
    )

    profile_dict = profile.to_dict()

    # Check dict representation. Note that disabled=False is not in the dict.
    expected_dict = {
        "address": "some-address",
        "namespace": "some-namespace",
        "api_key": "some-api-key",
        "tls": {
            "server_name": "some-server-name",
            "server_ca_cert": {"data": "ca-cert-data"},
            "client_cert": {"path": str(Path("/path/to/client.crt"))},
            "client_key": {"data": "client-key-data"},
        },
        "grpc_meta": {"some-header": "some-value"},
    }
    assert profile_dict == expected_dict

    # Convert back to profile
    new_profile = ClientConfigProfile.from_dict(profile_dict)

    # We expect the new profile to be the same, but with bytes-based data
    # sources converted to strings. This is because to_dict converts
    # bytes-based data to a string, suitable for TOML. So we only have
    # a string representation to work with.
    expected_new_profile = ClientConfigProfile(
        address="some-address",
        namespace="some-namespace",
        api_key="some-api-key",
        tls=ClientConfigTLS(
            disabled=False,
            server_name="some-server-name",
            server_root_ca_cert="ca-cert-data",  # Was bytes, now str
            client_cert=Path("/path/to/client.crt"),
            client_private_key="client-key-data",
        ),
        grpc_meta={"some-header": "some-value"},
    )
    assert new_profile == expected_new_profile

    # Test with minimal profile
    profile_minimal = ClientConfigProfile()
    profile_minimal_dict = profile_minimal.to_dict()
    assert profile_minimal_dict == {}
    new_profile_minimal = ClientConfigProfile.from_dict(profile_minimal_dict)
    assert profile_minimal == new_profile_minimal


def test_client_config_to_from_dict():
    """Test round-trip ClientConfig to and from a dictionary."""
    # Config with multiple profiles
    profile1 = ClientConfigProfile(
        address="some-address",
        namespace="some-namespace",
    )
    profile2 = ClientConfigProfile(
        address="another-address",
        tls=ClientConfigTLS(server_name="some-server-name"),
        grpc_meta={"some-header": "some-value"},
    )
    config = ClientConfig(profiles={"default": profile1, "custom": profile2})

    config_dict = config.to_dict()

    expected_dict = {
        "default": {
            "address": "some-address",
            "namespace": "some-namespace",
        },
        "custom": {
            "address": "another-address",
            "tls": {"server_name": "some-server-name"},
            "grpc_meta": {"some-header": "some-value"},
        },
    }
    assert config_dict == expected_dict

    # Convert back to config
    new_config = ClientConfig.from_dict(config_dict)
    assert config == new_config

    # Test empty config
    empty_config = ClientConfig(profiles={})
    empty_config_dict = empty_config.to_dict()
    assert empty_config_dict == {}
    new_empty_config = ClientConfig.from_dict(empty_config_dict)
    assert empty_config == new_empty_config
