"""Environment and file-based configuration for Temporal clients.

This module provides utilities to load Temporal client configuration from TOML files
and environment variables.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Union

from typing_extensions import TypeAlias

# from temporalio.service import ConnectConfig, TLSConfig
import temporalio.service
from temporalio.bridge.temporal_sdk_bridge import envconfig as _bridge_envconfig

DataSource: TypeAlias = Union[
    str, bytes
]  # str represents a file path, bytes represents raw data


def _from_dict_to_source(d: Optional[Mapping[str, Any]]) -> Optional[DataSource]:
    if not d:
        return None
    if "data" in d:
        return d["data"]
    if "path" in d:
        return d["path"]
    return None


def _read_source(source: Optional[DataSource]) -> Optional[bytes]:
    if not source:
        return None
    if isinstance(source, str):
        with open(Path(source), "rb") as f:
            return f.read()
    return source


@dataclass(frozen=True)
class ClientConfigTLS:
    """TLS configuration as specified as part of client configuration

    .. warning::
        Experimental API.
    """

    disabled: bool = False
    """If true, TLS is explicitly disabled."""
    server_name: Optional[str] = None
    """SNI override."""
    server_root_ca_cert: Optional[DataSource] = None
    """Server CA certificate source."""
    client_cert: Optional[DataSource] = None
    """Client certificate source."""
    client_private_key: Optional[DataSource] = None
    """Client key source."""

    def to_connect_tls_config(self) -> Union[bool, temporalio.service.TLSConfig]:
        """Create a `temporalio.service.TLSConfig` from this profile."""
        if self.disabled:
            return False

        return temporalio.service.TLSConfig(
            domain=self.server_name,
            server_root_ca_cert=_read_source(self.server_root_ca_cert),
            client_cert=_read_source(self.client_cert),
            client_private_key=_read_source(self.client_private_key),
        )

    @staticmethod
    def _from_dict(d: Optional[Mapping[str, Any]]) -> Optional[ClientConfigTLS]:
        if not d:
            return None
        return ClientConfigTLS(
            disabled=d.get("disabled", False),
            server_name=d.get("server_name"),
            # Note: Bridge uses snake_case, but TOML uses kebab-case which is
            # converted to snake_case. Core has server_ca_cert, client_key.
            server_root_ca_cert=_from_dict_to_source(d.get("server_ca_cert")),
            client_cert=_from_dict_to_source(d.get("client_cert")),
            client_private_key=_from_dict_to_source(d.get("client_key")),
        )


@dataclass(frozen=True)
class ClientConfigProfile:
    """Represents a client configuration profile.

    This class holds the configuration as loaded from a file or environment.
    See `to_connect_config` to transform the profile to `temporalio.service.ConnectConfig`,
    which can be used to create a client.

    .. warning::
        Experimental API.
    """

    address: Optional[str] = None
    """Client address."""
    namespace: Optional[str] = None
    """Client namespace."""
    api_key: Optional[str] = None
    """Client API key."""
    tls: Optional[ClientConfigTLS] = None
    """TLS configuration."""
    grpc_meta: Mapping[str, str] = field(default_factory=dict)
    """gRPC metadata."""

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> ClientConfigProfile:
        """Create a ClientConfigProfile from a dictionary."""
        return ClientConfigProfile(
            address=d.get("address"),
            namespace=d.get("namespace"),
            api_key=d.get("api_key"),
            tls=ClientConfigTLS._from_dict(d.get("tls")),
            grpc_meta=d.get("grpc_meta") or {},
        )

    def to_connect_config(self) -> temporalio.service.ConnectConfig:
        """Create a `temporalio.service.ConnectConfig` from this profile."""
        # Create a dictionary of kwargs for ConnectConfig
        kwargs: dict[str, Any] = {"api_key": self.api_key}

        if self.address:
            kwargs["target_host"] = self.address

        rpc_metadata = dict(self.grpc_meta)
        if rpc_metadata:
            kwargs["rpc_metadata"] = rpc_metadata

        if self.tls:
            kwargs["tls"] = self.tls.to_connect_tls_config()

        return temporalio.service.ConnectConfig(
            **{k: v for k, v in kwargs.items() if v is not None}
        )


@dataclass
class ClientConfig:
    """Client configuration loaded from TOML and environment variables.

    This contains a mapping of profile names to client profiles. Use
    `ClientConfigProfile.to_connect_config` to create a `temporalio.service.ConnectConfig`
    from a profile. See `load_profile` to load an individual profile.

    .. warning::
        Experimental API.
    """

    profiles: Mapping[str, ClientConfigProfile]
    """Map of profile name to its corresponding ClientConfigProfile."""

    @staticmethod
    def _from_bridge_profiles(
        bridge_profiles: Mapping[str, Mapping[str, Any]],
    ) -> ClientConfig:
        return ClientConfig(
            profiles={
                k: ClientConfigProfile.from_dict(v) for k, v in bridge_profiles.items()
            }
        )

    @staticmethod
    def load_profiles(
        *,
        disable_file: bool = False,
        config_file_strict: bool = False,
        env_vars: Optional[Mapping[str, str]] = None,
    ) -> ClientConfig:
        """Load all client profiles from default file locations and environment variables.

        This does not apply environment variable overrides to the profiles, it
        only uses an environment variable to find the default config file path
        (`TEMPORAL_CONFIG_FILE`). To get a single profile with environment variables
        applied, use `load_profile`.

        Args:
            disable_file: If true, file loading is disabled. Will create a default
                configuration.
            config_file_strict: If true, will TOML file parsing will error on
                unrecognized keys.
            env_vars: The environment variables to use for locating the default config
                file. If not provided, `TEMPORAL_CONFIG_FILE` is not checked
                and only the default path is used (./temporal/temporal.toml). To use
                the current process's environment, `os.environ` can be passed explicitly.
        """
        loaded_profiles = _bridge_envconfig.load_client_config(
            disable_file=disable_file,
            config_file_strict=config_file_strict,
            env_vars=env_vars,
        )
        return ClientConfig._from_bridge_profiles(loaded_profiles)

    @staticmethod
    def load_profiles_from_file(
        config_file: str,
        *,
        config_file_strict: bool = False,
    ) -> ClientConfig:
        """Load all client profiles from a specific file."""
        loaded_profiles = _bridge_envconfig.load_client_config_from_file(
            path=config_file,
            config_file_strict=config_file_strict,
        )
        return ClientConfig._from_bridge_profiles(loaded_profiles)

    @staticmethod
    def load_profiles_from_data(
        config_file_data: Union[str, bytes],
        *,
        config_file_strict: bool = False,
    ) -> ClientConfig:
        """Load all client profiles from specific data."""
        data_bytes = (
            config_file_data.encode("utf-8")
            if isinstance(config_file_data, str)
            else config_file_data
        )
        loaded_profiles = _bridge_envconfig.load_client_config_from_data(
            data=data_bytes,
            config_file_strict=config_file_strict,
        )
        return ClientConfig._from_bridge_profiles(loaded_profiles)

    @staticmethod
    def load_profile(
        profile: str = "default",
        *,
        disable_file: bool = False,
        disable_env: bool = False,
        config_file_strict: bool = False,
        env_vars: Optional[Mapping[str, str]] = None,
    ) -> ClientConfigProfile:
        """Load a single client profile from default sources, applying env
        overrides.

        To get a `temporalio.service.ConnectConfig`, use the
        `ClientConfigProfile.to_connect_config` method on the returned profile.

        Args:
            profile: Profile to load from the config.
            disable_file: If true, file loading is disabled.
            disable_env: If true, environment variable loading and overriding
                is disabled. This takes precedence over the ``env_vars``
                parameter.
            config_file_strict: If true, will error on unrecognized keys.
            env_vars: The environment to use for loading and overrides. If not
                provided, environment variables are not used for overrides. To
                use the current process's environment, `os.environ` can be
                passed explicitly.

        Returns:
            The client configuration profile.
        """
        if disable_file and disable_env:
            raise ValueError("Cannot disable both file and environment loading")

        raw_profile = _bridge_envconfig.load_client_connect_config(
            profile=profile,
            disable_file=disable_file,
            disable_env=disable_env,
            config_file_strict=config_file_strict,
            env_vars=env_vars,
        )
        return ClientConfigProfile.from_dict(raw_profile)

    @staticmethod
    def load_profile_from_file(
        config_file: str,
        profile: str = "default",
        *,
        disable_env: bool = False,
        config_file_strict: bool = False,
        env_vars: Optional[Mapping[str, str]] = None,
    ) -> ClientConfigProfile:
        """Load a single client profile from a file, applying env overrides.

        To get a `temporalio.service.ConnectConfig`, use the
        `ClientConfigProfile.to_connect_config` method on the returned profile.

        Args:
            config_file: Path to the TOML config file.
            profile: Profile to load from the config.
            disable_env: If true, environment variable overriding is disabled.
                This takes precedence over the `env_vars` parameter.
            config_file_strict: If true, will error on unrecognized keys.
            env_vars: The environment to use for overrides. If not provided,
                environment variables are not used for overrides. To use the
                current process's environment, `os.environ` can be
                passed explicitly.
        """
        raw_profile = _bridge_envconfig.load_client_connect_config_from_file(
            profile=profile,
            path=config_file,
            disable_env=disable_env,
            config_file_strict=config_file_strict,
            env_vars=env_vars,
        )
        return ClientConfigProfile.from_dict(raw_profile)

    @staticmethod
    def load_profile_from_data(
        config_file_data: Union[str, bytes],
        profile: str = "default",
        *,
        disable_env: bool = False,
        config_file_strict: bool = False,
        env_vars: Optional[Mapping[str, str]] = None,
    ) -> ClientConfigProfile:
        """Load a single client profile from data, applying env overrides.

        To get a `temporalio.service.ConnectConfig`, use the
        `ClientConfigProfile.to_connect_config` method on the returned profile.

        Args:
            config_file_data: Raw string TOML config.
            profile: Profile to load from the config.
            disable_env: If true, environment variable overriding is disabled.
                This takes precedence over the ``env_vars`` parameter.
            config_file_strict: If true, will error on unrecognized keys.
            env_vars: The environment to use for overrides. If not provided,
                environment variables are not used for overrides. To use the
                current process's environment, `os.environ` can be
                passed explicitly.
        """
        data_bytes = (
            config_file_data.encode("utf-8")
            if isinstance(config_file_data, str)
            else config_file_data
        )
        raw_profile = _bridge_envconfig.load_client_connect_config_from_data(
            profile=profile,
            data=data_bytes,
            disable_env=disable_env,
            config_file_strict=config_file_strict,
            env_vars=env_vars,
        )
        return ClientConfigProfile.from_dict(raw_profile)
