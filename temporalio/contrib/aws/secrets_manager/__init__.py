"""Client config extension that loads Temporal client credentials (API key and/or
TLS material) from AWS Secrets Manager, so that only secret IDs, never secret
material, need to live in static config.

Each credential is configured with a Secrets Manager secret ID (name or full
ARN), either in the TOML config file::

    [profile.default.ext.aws_secrets_manager]
    api_key_id = "my-temporal-api-key"

    [profile.default.ext.aws_secrets_manager.tls]
    client_cert_id = "my-temporal-client-cert"
    client_key_id = "my-temporal-client-key"
    server_ca_cert_id = "my-temporal-server-ca-cert"

or with environment variables, which take precedence over the file::

    TEMPORAL_EXT_AWS_SECRETS_MANAGER_API_KEY_ID
    TEMPORAL_EXT_AWS_SECRETS_MANAGER_TLS_CLIENT_CERT_ID
    TEMPORAL_EXT_AWS_SECRETS_MANAGER_TLS_CLIENT_KEY_ID
    TEMPORAL_EXT_AWS_SECRETS_MANAGER_TLS_SERVER_CA_CERT_ID

Usage::

    from temporalio.client import Client
    from temporalio.contrib.aws.secrets_manager import SecretsManagerExtension
    from temporalio.envconfig import ClientConfig

    config = ClientConfig.load_client_connect_config(
        extensions=[SecretsManagerExtension()]
    )
    client = await Client.connect(**config)

Secrets are fetched through the AWS Parameters and Secrets Lambda Extension when
it is detected, and otherwise directly from Secrets Manager with boto3, which
requires the ``boto3`` extra (e.g. ``temporalio[boto3]``). See the README for
the validation rules, how the Lambda extension is detected, caching behavior,
and metrics.

.. warning::
   This API is experimental and unstable.
"""

from temporalio.contrib.aws.secrets_manager._extension import (
    EXTENSION_NAME,
    SecretsManagerExtension,
)
from temporalio.contrib.aws.secrets_manager._fetch import SecretsClient

__all__ = [
    "EXTENSION_NAME",
    "SecretsClient",
    "SecretsManagerExtension",
]
