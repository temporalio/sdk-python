# AWS Secrets Manager for Temporal Client Config

> ⚠️ **This package is currently at an experimental release stage.** ⚠️

`temporalio.contrib.aws.secrets_manager` is a
[`temporalio.envconfig`](../../../envconfig.py) extension that loads Temporal
client credentials (API key and/or mTLS material) from
[AWS Secrets Manager](https://aws.amazon.com/secrets-manager/). Only secret IDs
live in your config. Secret material is fetched at runtime and kept in memory.

## Install

If the
[AWS Parameters and Secrets Lambda Extension](https://docs.aws.amazon.com/secretsmanager/latest/userguide/retrieving-secrets_lambda.html)
is available (see [Fetching and caching](#fetching-and-caching)), no extra
dependencies are needed. Otherwise, secrets are fetched directly with
[boto3](https://github.com/boto/boto3). Install the `boto3` extra for this:

    python -m pip install "temporalio[boto3]"

## Usage

```python
from temporalio.client import Client
from temporalio.contrib.aws.secrets_manager import SecretsManagerExtension
from temporalio.envconfig import ClientConfig

config = ClientConfig.load_client_connect_config(
    extensions=[SecretsManagerExtension()],
)
client = await Client.connect(**config)
```

The extension adds a client plugin to the returned config's `plugins`. To pass
other plugins to `Client.connect`, combine them with these rather than passing
`plugins` separately.

## Configuration

Each credential is configured with a Secrets Manager secret ID (friendly name
or full ARN), either in the TOML config file or with environment variables.
Environment variables take precedence over the file.

| TOML (under `profile.<profile>.ext.aws_secrets_manager`) | Environment variable | Replaces |
|---|---|---|
| `api_key_id` | `TEMPORAL_EXT_AWS_SECRETS_MANAGER_API_KEY_ID` | `api_key` / `TEMPORAL_API_KEY` |
| `tls.client_cert_id` | `TEMPORAL_EXT_AWS_SECRETS_MANAGER_TLS_CLIENT_CERT_ID` | `tls.client_cert_{path,data}` / `TEMPORAL_TLS_CLIENT_CERT_{PATH,DATA}` |
| `tls.client_key_id` | `TEMPORAL_EXT_AWS_SECRETS_MANAGER_TLS_CLIENT_KEY_ID` | `tls.client_key_{path,data}` / `TEMPORAL_TLS_CLIENT_KEY_{PATH,DATA}` |
| `tls.server_ca_cert_id` | `TEMPORAL_EXT_AWS_SECRETS_MANAGER_TLS_SERVER_CA_CERT_ID` | `tls.server_ca_cert_{path,data}` / `TEMPORAL_TLS_SERVER_CA_CERT_{PATH,DATA}` |

For example:

```toml
[profile.default]
address = "my-namespace.a1b2c.tmprl.cloud:7233"
namespace = "my-namespace.a1b2c"

[profile.default.ext.aws_secrets_manager.tls]
client_cert_id = "temporal/client-cert-and-key"
client_key_id = "temporal/client-cert-and-key"
```

Rules:

- A secret ID can't be combined with the credential it replaces, from any
  source. For example, `api_key` in the file with
  `TEMPORAL_EXT_AWS_SECRETS_MANAGER_API_KEY_ID` in the environment is an error.
- The client cert and key IDs must be set together. They may be the same ID,
  in which case the secret holds both the PEM certificate and the PEM key. This
  lets the pair be rotated atomically.
- TLS secret IDs can't be used when TLS is disabled.
- An API key secret enables TLS by default, as `api_key` does.
- Secret values are used as-is: the API key as a string, and TLS material as
  PEM. Both `SecretString` and `SecretBinary` secrets are supported.
- Unknown keys under `ext.aws_secrets_manager` are rejected.

## Fetching and caching

When `SecretsManagerExtension.secrets_client` is unset, secrets are fetched
through the
[AWS Parameters and Secrets Lambda Extension](https://docs.aws.amazon.com/secretsmanager/latest/userguide/retrieving-secrets_lambda.html)
if it is present. Otherwise they are fetched directly from Secrets Manager with
a boto3 client built from the default AWS config, which requires the `boto3`
extra.

The Lambda extension is detected when the config is loaded, as follows:

- If `PARAMETERS_SECRETS_EXTENSION_HTTP_PORT` is set to a port, the extension
  is used on that port.
- If it is unset and the code is running in Lambda (`AWS_LAMBDA_FUNCTION_NAME`
  is set), the extension's default port, 2773, is probed, and the extension is
  used if something is listening there. Outside Lambda, the port is not probed
  and secrets are fetched directly.
- Setting `PARAMETERS_SECRETS_EXTENSION_HTTP_PORT` to an empty value explicitly
  disables detection so that secrets are always fetched directly. Use this if
  another service in your function listens on port 2773.

To use your own client, for example with a specific region or credentials, set
`secrets_client`. It is called from a worker thread, so it must be
thread-safe, as boto3 clients are:

```python
import boto3

ext = SecretsManagerExtension(
    secrets_client=boto3.client("secretsmanager", region_name="us-west-2"),
)
```

Secrets are first fetched when a client connects, and connecting fails if a
fetch fails. After that:

- Secrets are cached by the plugin that the extension adds to the loaded
  config, so clients connected with the same config share the cache (for
  example, across warm Lambda invocations). The cache isn't tied to an event
  loop, so the clients may run on different loops.
- The API key is refreshed in the background while a client is connected, so
  a rotated API key is picked up without reconnecting. This happens every
  refresh interval, but at most every 30 seconds.
- TLS material (client cert/key and server CA cert) is fetched only when a
  client connects. A rotated client cert is picked up the next time a client
  connects with the same config.
- With separate client cert and key secrets, a fetch partway through rotating
  them can get a cert and key that don't match, and connecting fails until
  they're next refreshed. Use a single secret holding both to avoid this.
- The refresh interval is `SecretsManagerExtension.refresh_interval` if set.
  Otherwise it comes from the `SECRETS_MANAGER_TTL` environment variable,
  interpreted the same way as by the Lambda extension: whole seconds, where 0
  disables caching. Unlike the Lambda extension, values over 300 are allowed.
  If neither is set, it defaults to 5 minutes. An invalid `SECRETS_MANAGER_TTL`
  is an error when loading the config.
- If a refresh fails, the last fetched value keeps being used for up to
  `SecretsManagerExtension.max_staleness` (default 24 hours) after it was
  fetched. After a failed refresh, the next fetch attempt waits 30 seconds.

## Observability

The extension logs to the `temporalio.contrib.aws.secrets_manager` logger and
emits metrics to the metric meter of the client's runtime.

Metrics are all labeled with `secret_type` (`api_key`, `tls_client_cert`,
`tls_server_ca_cert`; client cert and client key metrics are both labeled
`tls_client_cert`) and `fetch_path` (`extension` or `sdk`). Currently, the
following metrics are emitted:

| Metric | Type | Extra labels | Description |
|---|---|---|---|
| `temporal_aws_secrets_manager_fetch_failed` | Counter | `failure_reason`, `fallback` (`stale`, `none`) | Failed fetches, and whether the previous value was used instead |
| `temporal_aws_secrets_manager_fetch_latency` | Histogram | `outcome` (`success`, `failure`) | Latency of each fetch |
| `temporal_aws_secrets_manager_cache_lookup` | Counter | `result` (`hit`, `miss`, `miss_with_fallback`) | Lookups that returned a value |

## Required IAM permissions

The AWS credentials in use (for example, the Lambda execution role) need
`secretsmanager:GetSecretValue` on each configured secret:

```json
{
  "Effect": "Allow",
  "Action": "secretsmanager:GetSecretValue",
  "Resource": "arn:aws:secretsmanager:us-west-2:123456789012:secret:temporal/*"
}
```

If a secret is encrypted with a customer-managed KMS key, `kms:Decrypt` on that
key is also required.
