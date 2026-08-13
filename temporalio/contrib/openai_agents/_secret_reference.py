"""References to secrets held in the worker process environment.

A resolved secret is never written back into the activity's own input: the
resolvers copy at every level they write to, so the input keeps the marker.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping, MutableMapping
from typing import Any, cast

from agents.tool import ShellToolContainerAutoEnvironment, ShellToolEnvironment
from openai.types.responses.tool_param import CodeInterpreter, Mcp
from pydantic import ValidationError

from temporalio.contrib.openai_agents.workflow import AgentsWorkflowError
from temporalio.exceptions import ApplicationError

_MARKER_PREFIX = "temporal.secret_reference:"

_ERROR_TYPE = "SecretReferenceFailure"


def secret_reference(key: str) -> str:
    """Refer to a secret held in the worker process environment.

    .. warning::
        This function is experimental and may change in future versions.
        Use with caution in production environments.

    A hosted tool's credential is sent from workflow code to the model, so
    writing the credential into the tool config puts the credential itself in
    the workflow. Pass the *name of an environment variable* here instead, and
    put the placeholder returned where the credential would have gone::

        from agents import HostedMCPTool

        from temporalio.contrib.openai_agents import secret_reference

        tool = HostedMCPTool(
            tool_config={
                "type": "mcp",
                "server_label": "my_server",
                "server_url": "https://example.com/mcp",
                "authorization": secret_reference("MY_MCP_TOKEN"),
            }
        )

    The worker reads the variable from its own environment and substitutes its
    value for the placeholder immediately before the model call.

    Set the variable on every worker that runs model activities; a worker
    without a value for it fails the model call with a non-retryable
    ``ApplicationError`` of type ``SecretReferenceFailure``, naming the
    variable.

    The placeholder returned is the string
    ``"temporal.secret_reference:<key>"``. It is substituted in a hosted MCP
    tool's ``authorization`` and in the value of each of its ``headers``, and in
    the ``value`` of each domain secret under a hosted shell or code interpreter
    container's ``network_policy``. Anywhere else — in a header *name*, or as an
    MCP server ``factory_argument`` — it reaches the receiving system as that
    literal string. Nothing in this SDK validates or complains about one that
    was not substituted; you find out from whatever that system does with text
    that is not a credential, typically a failed authentication.

    Args:
        key: Name of the environment variable to read on the worker.

    Returns:
        A placeholder string to use in place of the secret.

    Raises:
        AgentsWorkflowError: If ``key`` is empty.
    """
    if not key:
        raise AgentsWorkflowError(
            "secret_reference() requires the name of an environment variable to read "
            "on the worker, but the name given was empty."
        )
    return _MARKER_PREFIX + key


def _resolve_secret_reference(value: str) -> str:
    """Return ``value`` with a secret reference marker replaced by its secret.

    Raises:
        ApplicationError: If the marker names no environment variable, or the
            variable it names is unset or empty in the worker process
            environment. Non-retryable, of type ``SecretReferenceFailure``.
    """
    if not value.startswith(_MARKER_PREFIX):
        return value
    key = value[len(_MARKER_PREFIX) :]
    if not key:
        raise ApplicationError(
            f"Malformed secret reference {value!r}: the text after "
            f"{_MARKER_PREFIX!r} must be the name of an environment variable to read "
            "on the worker. Build the placeholder with secret_reference().",
            type=_ERROR_TYPE,
            non_retryable=True,
        )
    secret = os.environ.get(key)
    if not secret:
        raise ApplicationError(
            f"Secret reference environment variable {key!r} is not set, or is empty, "
            "in the worker process environment.",
            type=_ERROR_TYPE,
            non_retryable=True,
        )
    return secret


def _shallow_copy(mapping: Any) -> Any:
    return dict(cast(Mapping[str, Any], mapping))


def _malformed_domain_secret_error(e: ValidationError) -> ApplicationError:
    """The rejection to raise for a domain secret that does not validate.

    pydantic rejects the whole entry for some malformed shapes and a single
    field for others, so the type named is not claimed to be the entry's.
    """
    error = e.errors()[0]
    return ApplicationError(
        f"Domain secret {error['loc'][0]} in a container network policy is "
        f"malformed. Only its position and the type of the value that was "
        f"rejected ({type(error['input']).__name__}) are reported: a malformed "
        "entry could itself hold the secret.",
        type=_ERROR_TYPE,
        non_retryable=True,
    )


class _UnreadDomainSecrets:
    """Raises when iterated, so secrets a failed read consumed never read as absent."""

    def __init__(self, error: ApplicationError) -> None:
        self._error = error

    def __iter__(self) -> Iterator[Any]:
        raise self._error


def _resolve_network_policy(network_policy: Any) -> Any:
    """Copy a container network policy, resolving each domain secret value.

    On the code interpreter path pydantic deserializes ``domain_secrets`` into a
    single-pass iterator, so the entries read here go back onto the input.

    Raises:
        ApplicationError: If a marker cannot be resolved, or a domain secret is
            malformed. Non-retryable.
    """
    policy = cast(MutableMapping[str, Any], network_policy)
    domain_secrets = policy.get("domain_secrets")
    if domain_secrets is None:
        return dict(policy)
    try:
        unresolved = list(domain_secrets)
    except ValidationError as e:
        error = _malformed_domain_secret_error(e)
        policy["domain_secrets"] = _UnreadDomainSecrets(error)
        # Not chained: the validation error carries the entry it rejected.
        raise error from None
    policy["domain_secrets"] = unresolved
    resolved = dict(policy)
    resolved["domain_secrets"] = [
        _resolve_domain_secret(secret) for secret in unresolved
    ]
    return resolved


def _resolve_domain_secret(secret: Mapping[str, Any]) -> dict[str, Any]:
    return {**secret, "value": _resolve_secret_reference(secret["value"])}


def resolve_mcp_tool_config(tool_config: Mcp) -> Mcp:
    """Copy a hosted MCP tool config, resolving its authorization and headers.

    Raises:
        ApplicationError: If a marker cannot be resolved. Non-retryable.
    """
    resolved = _shallow_copy(tool_config)
    if "authorization" in tool_config:
        resolved["authorization"] = _resolve_secret_reference(
            tool_config["authorization"]
        )
    headers = tool_config.get("headers")
    if headers is not None:
        resolved["headers"] = {
            name: _resolve_secret_reference(value) for name, value in headers.items()
        }
    return resolved


def resolve_shell_tool_environment(
    environment: ShellToolEnvironment | None,
) -> ShellToolEnvironment:
    """Copy a shell tool environment, resolving its domain secret values.

    An absent environment comes back as the local one ``ShellTool`` normalizes it to.

    Raises:
        ApplicationError: If a marker cannot be resolved, or a domain secret is
            malformed. Non-retryable.
    """
    if environment is None:
        return {"type": "local"}
    if environment.get("type") != "container_auto":
        return _shallow_copy(environment)
    auto = cast(ShellToolContainerAutoEnvironment, environment)
    network_policy = auto.get("network_policy")
    resolved = _shallow_copy(auto)
    if network_policy is not None:
        resolved["network_policy"] = _resolve_network_policy(network_policy)
    return resolved


def resolve_code_interpreter_tool_config(
    tool_config: CodeInterpreter,
) -> CodeInterpreter:
    """Copy a code interpreter tool config, resolving its domain secret values.

    Raises:
        ApplicationError: If a marker cannot be resolved, or a domain secret is
            malformed. Non-retryable.
    """
    resolved = _shallow_copy(tool_config)
    container = tool_config.get("container")
    if not isinstance(container, Mapping):
        return resolved
    network_policy = container.get("network_policy")
    if network_policy is None:
        return resolved
    resolved_container = _shallow_copy(container)
    resolved_container["network_policy"] = _resolve_network_policy(network_policy)
    resolved["container"] = resolved_container
    return resolved
