"""Tests for secret references in hosted tool secrets."""

import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from agents import (
    AgentOutputSchemaBase,
    CodeInterpreterTool,
    Handoff,
    HostedMCPTool,
    Model,
    ModelResponse,
    ModelSettings,
    ModelTracing,
    Tool,
    TResponseInputItem,
    Usage,
)
from agents.items import TResponseStreamEvent
from agents.tool import ShellTool, ShellToolEnvironment

from temporalio import workflow
from temporalio.api.failure.v1 import Failure
from temporalio.client import Client
from temporalio.contrib.openai_agents import (
    AgentsWorkflowError,
    ModelActivityParameters,
    OpenAIPayloadConverter,
    secret_reference,
)
from temporalio.contrib.openai_agents._invoke_model_activity import (
    ActivityModelInput,
    ModelActivity,
    StreamingActivityModelInput,
    _build_tool,
)
from temporalio.contrib.openai_agents._temporal_model_stub import _TemporalModelStub
from temporalio.contrib.openai_agents.testing import (
    AgentEnvironment,
    TestModel,
    TestModelProvider,
)
from temporalio.converter import DefaultFailureConverter, PayloadConverter
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from tests.helpers import new_worker

# Fabricated secrets. Neither may reach a serialized activity argument.
SENTINEL = "sk-test-sentinel-4f1a9c7e2b"
ENV_KEY = "TEMPORAL_TEST_TOOL_SECRET"
OTHER_SENTINEL = "sk-test-other-8c3d5e0a1f"
OTHER_ENV_KEY = "TEMPORAL_TEST_OTHER_TOOL_SECRET"


def _round_trip_activity_input(tool: Tool) -> tuple[bytes, ActivityModelInput]:
    """Serialize the activity arguments a workflow would send for ``tool``.

    Returns the serialized payload bytes and the input as the activity receives
    it after deserialization.
    """
    stub = _TemporalModelStub(
        model_name="gpt-5",
        model_params=ModelActivityParameters(),
        agent=None,
    )
    activity_input, _summary = stub._build_activity_input(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[tool],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )
    converter = OpenAIPayloadConverter()
    payload = converter.to_payload(activity_input)
    return payload.data, converter.from_payload(payload, ActivityModelInput)


def _activity_input_payload_and_tool(tool: Tool) -> tuple[bytes, Any]:
    """The serialized payload, and the single tool the activity receives."""
    payload, received = _round_trip_activity_input(tool)
    tools = received.get("tools") or []
    assert len(tools) == 1
    return payload, tools[0]


def _hosted_mcp_tool(authorization: str, header_value: str) -> HostedMCPTool:
    return HostedMCPTool(
        tool_config={
            "type": "mcp",
            "server_label": "test_server",
            "server_url": "https://example.com/mcp",
            "authorization": authorization,
            "headers": {"X-Token": header_value, "X-Plain": "not-a-secret"},
        }
    )


def _domain_secret(name: str, value: str) -> dict[str, str]:
    return {"domain": "example.com", "name": name, "value": value}


def _allowlist(domain_secrets: tuple[Any, ...]) -> Any:
    return {
        "type": "allowlist",
        "allowed_domains": ["example.com"],
        "domain_secrets": list(domain_secrets),
    }


def _shell_tool(*domain_secrets: Any) -> ShellTool:
    environment: Any = {
        "type": "container_auto",
        "network_policy": _allowlist(domain_secrets),
    }
    return ShellTool(environment=environment)


def _code_interpreter_tool(*domain_secrets: Any) -> CodeInterpreterTool:
    tool_config: Any = {
        "type": "code_interpreter",
        "container": {
            "type": "auto",
            "network_policy": _allowlist(domain_secrets),
        },
    }
    return CodeInterpreterTool(tool_config=tool_config)


def _fields(value: Any) -> dict[str, Any]:
    """View a TypedDict-shaped value as a plain mapping, for assertions."""
    return cast(dict[str, Any], value)


def _domain_secrets(network_policy: Any) -> list[Any]:
    return list(_fields(network_policy)["domain_secrets"])


def _reported_failure(error: BaseException) -> Failure:
    """The failure a worker would report to the server for ``error``.

    The converter walks ``__cause__``, or the implicit ``__context__`` when
    there is none, into ``failure.cause``.
    """
    failure = Failure()
    DefaultFailureConverter().to_failure(error, PayloadConverter.default, failure)
    return failure


def _shell_secrets(built: Tool) -> list[Any]:
    assert isinstance(built, ShellTool)
    assert built.environment is not None
    return _domain_secrets(_fields(built.environment)["network_policy"])


def _code_interpreter_secrets(built: Tool) -> list[Any]:
    assert isinstance(built, CodeInterpreterTool)
    container = _fields(built.tool_config)["container"]
    return _domain_secrets(_fields(container)["network_policy"])


def test_hosted_mcp_secrets_stay_out_of_activity_arguments(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_KEY, SENTINEL)
    marker = secret_reference(ENV_KEY)

    payload, received = _activity_input_payload_and_tool(
        _hosted_mcp_tool(marker, marker)
    )

    assert SENTINEL.encode() not in payload
    assert payload.count(marker.encode()) == 2
    assert received.tool_config["authorization"] == marker
    assert received.tool_config["headers"]["X-Token"] == marker


def test_hosted_shell_domain_secret_stays_out_of_activity_arguments(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_KEY, SENTINEL)
    marker = secret_reference(ENV_KEY)

    payload, received = _activity_input_payload_and_tool(
        _shell_tool(_domain_secret("TOKEN", marker))
    )

    assert SENTINEL.encode() not in payload
    assert marker.encode() in payload
    secrets = _domain_secrets(received.environment["network_policy"])
    assert secrets[0]["value"] == marker


def test_code_interpreter_domain_secret_stays_out_of_activity_arguments(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_KEY, SENTINEL)
    marker = secret_reference(ENV_KEY)

    payload, received = _activity_input_payload_and_tool(
        _code_interpreter_tool(_domain_secret("TOKEN", marker))
    )

    assert SENTINEL.encode() not in payload
    assert marker.encode() in payload
    secrets = _domain_secrets(received.tool_config["container"]["network_policy"])
    assert secrets[0]["value"] == marker


def test_hosted_mcp_secrets_resolve_for_the_model_call(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_KEY, SENTINEL)
    marker = secret_reference(ENV_KEY)
    _payload, received = _activity_input_payload_and_tool(
        _hosted_mcp_tool(marker, marker)
    )

    built = _build_tool(received)

    assert isinstance(built, HostedMCPTool)
    assert _fields(built.tool_config)["authorization"] == SENTINEL
    assert _fields(built.tool_config)["headers"] == {
        "X-Token": SENTINEL,
        "X-Plain": "not-a-secret",
    }
    # The deserialized activity argument still holds the marker, so building
    # again resolves the same secret rather than an emptied config.
    assert received.tool_config["authorization"] == marker
    assert received.tool_config["headers"]["X-Token"] == marker


def test_hosted_shell_domain_secret_resolves_for_the_model_call(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_KEY, SENTINEL)
    marker = secret_reference(ENV_KEY)
    _payload, received = _activity_input_payload_and_tool(
        _shell_tool(_domain_secret("TOKEN", marker))
    )

    built = _build_tool(received)

    assert _shell_secrets(built) == [_domain_secret("TOKEN", SENTINEL)]


def test_code_interpreter_domain_secret_resolves_for_the_model_call(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_KEY, SENTINEL)
    marker = secret_reference(ENV_KEY)
    _payload, received = _activity_input_payload_and_tool(
        _code_interpreter_tool(_domain_secret("TOKEN", marker))
    )

    built = _build_tool(received)

    assert _code_interpreter_secrets(built) == [_domain_secret("TOKEN", SENTINEL)]


def test_code_interpreter_domain_secret_survives_a_second_build(
    monkeypatch: pytest.MonkeyPatch,
):
    """``domain_secrets`` deserializes into a single-pass iterator here.

    Building twice from one deserialized input must resolve the secret both
    times, and must leave the input itself holding the marker.
    """
    monkeypatch.setenv(ENV_KEY, SENTINEL)
    marker = secret_reference(ENV_KEY)
    _payload, received = _activity_input_payload_and_tool(
        _code_interpreter_tool(_domain_secret("TOKEN", marker))
    )

    first = _build_tool(received)
    second = _build_tool(received)

    assert _code_interpreter_secrets(first) == [_domain_secret("TOKEN", SENTINEL)]
    assert _code_interpreter_secrets(second) == [_domain_secret("TOKEN", SENTINEL)]
    assert _domain_secrets(received.tool_config["container"]["network_policy"]) == [
        _domain_secret("TOKEN", marker)
    ]


def test_hosted_shell_domain_secret_survives_a_second_build(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_KEY, SENTINEL)
    marker = secret_reference(ENV_KEY)
    _payload, received = _activity_input_payload_and_tool(
        _shell_tool(_domain_secret("TOKEN", marker))
    )

    first = _build_tool(received)
    second = _build_tool(received)

    assert _shell_secrets(first) == [_domain_secret("TOKEN", SENTINEL)]
    assert _shell_secrets(second) == [_domain_secret("TOKEN", SENTINEL)]
    assert _domain_secrets(received.environment["network_policy"]) == [
        _domain_secret("TOKEN", marker)
    ]


def test_only_marker_domain_secrets_are_resolved(monkeypatch: pytest.MonkeyPatch):
    """A literal value alongside a marker is left exactly as the workflow sent it."""
    monkeypatch.setenv(ENV_KEY, SENTINEL)
    marker = secret_reference(ENV_KEY)
    literal = _domain_secret("PLAIN", "plain-token-value")
    _payload, received = _activity_input_payload_and_tool(
        _code_interpreter_tool(literal, _domain_secret("TOKEN", marker))
    )

    built = _build_tool(received)

    assert _code_interpreter_secrets(built) == [
        literal,
        _domain_secret("TOKEN", SENTINEL),
    ]


def test_two_secret_references_in_one_mcp_config_resolve_to_their_own_secrets(
    monkeypatch: pytest.MonkeyPatch,
):
    """Each reference names its own variable, and stands for that one only."""
    monkeypatch.setenv(ENV_KEY, SENTINEL)
    monkeypatch.setenv(OTHER_ENV_KEY, OTHER_SENTINEL)
    _payload, received = _activity_input_payload_and_tool(
        _hosted_mcp_tool(secret_reference(ENV_KEY), secret_reference(OTHER_ENV_KEY))
    )

    built = _build_tool(received)

    assert isinstance(built, HostedMCPTool)
    assert _fields(built.tool_config)["authorization"] == SENTINEL
    assert _fields(built.tool_config)["headers"] == {
        "X-Token": OTHER_SENTINEL,
        "X-Plain": "not-a-secret",
    }


def test_two_domain_secrets_resolve_to_their_own_secrets(
    monkeypatch: pytest.MonkeyPatch,
):
    """The entries are resolved one by one, each from the variable it names."""
    monkeypatch.setenv(ENV_KEY, SENTINEL)
    monkeypatch.setenv(OTHER_ENV_KEY, OTHER_SENTINEL)
    _payload, received = _activity_input_payload_and_tool(
        _code_interpreter_tool(
            _domain_secret("TOKEN", secret_reference(ENV_KEY)),
            _domain_secret("OTHER", secret_reference(OTHER_ENV_KEY)),
        )
    )

    built = _build_tool(received)

    assert _code_interpreter_secrets(built) == [
        _domain_secret("TOKEN", SENTINEL),
        _domain_secret("OTHER", OTHER_SENTINEL),
    ]


def test_shell_domain_secrets_resolve_by_position(monkeypatch: pytest.MonkeyPatch):
    """Each entry resolves from the variable it names, whichever position it holds."""
    monkeypatch.setenv(ENV_KEY, SENTINEL)
    monkeypatch.setenv(OTHER_ENV_KEY, OTHER_SENTINEL)
    literal = _domain_secret("PLAIN", "plain-token-value")
    _payload, received = _activity_input_payload_and_tool(
        _shell_tool(
            literal,
            _domain_secret("TOKEN", secret_reference(ENV_KEY)),
            _domain_secret("OTHER", secret_reference(OTHER_ENV_KEY)),
        )
    )

    built = _build_tool(received)

    assert _shell_secrets(built) == [
        literal,
        _domain_secret("TOKEN", SENTINEL),
        _domain_secret("OTHER", OTHER_SENTINEL),
    ]


def test_a_marker_in_a_header_name_is_passed_through(
    monkeypatch: pytest.MonkeyPatch,
):
    """A marker belongs where a credential belongs, and a header name is not that."""
    monkeypatch.setenv(ENV_KEY, SENTINEL)
    marker = secret_reference(ENV_KEY)
    tool_config: Any = {
        "type": "mcp",
        "server_label": "test_server",
        "server_url": "https://example.com/mcp",
        "headers": {marker: "not-a-secret"},
    }

    payload, received = _activity_input_payload_and_tool(
        HostedMCPTool(tool_config=tool_config)
    )

    built = _build_tool(received)

    assert isinstance(built, HostedMCPTool)
    assert _fields(built.tool_config)["headers"] == {marker: "not-a-secret"}
    assert SENTINEL.encode() not in payload


def test_an_mcp_config_without_an_authorization_resolves_its_headers(
    monkeypatch: pytest.MonkeyPatch,
):
    """``authorization`` is optional, and an absent one is not one to resolve."""
    monkeypatch.setenv(ENV_KEY, SENTINEL)
    tool_config: Any = {
        "type": "mcp",
        "server_label": "test_server",
        "server_url": "https://example.com/mcp",
        "headers": {"X-Token": secret_reference(ENV_KEY)},
    }

    _payload, received = _activity_input_payload_and_tool(
        HostedMCPTool(tool_config=tool_config)
    )

    built = _build_tool(received)

    assert isinstance(built, HostedMCPTool)
    assert "authorization" not in _fields(built.tool_config)
    assert _fields(built.tool_config)["headers"] == {"X-Token": SENTINEL}


def test_an_mcp_config_without_headers_resolves_its_authorization(
    monkeypatch: pytest.MonkeyPatch,
):
    """``headers`` is optional, and an absent one is not one to resolve."""
    monkeypatch.setenv(ENV_KEY, SENTINEL)
    tool_config: Any = {
        "type": "mcp",
        "server_label": "test_server",
        "server_url": "https://example.com/mcp",
        "authorization": secret_reference(ENV_KEY),
    }

    _payload, received = _activity_input_payload_and_tool(
        HostedMCPTool(tool_config=tool_config)
    )

    built = _build_tool(received)

    assert isinstance(built, HostedMCPTool)
    assert _fields(built.tool_config)["authorization"] == SENTINEL
    assert "headers" not in _fields(built.tool_config)


def test_local_shell_environment_keeps_its_executor():
    _payload, received = _activity_input_payload_and_tool(
        ShellTool(environment={"type": "local"}, executor=lambda _request: "")
    )

    built = _build_tool(received)

    assert isinstance(built, ShellTool)
    assert built.executor is not None
    assert _fields(built.environment) == {"type": "local"}


@pytest.mark.parametrize(
    "environment",
    [
        {"type": "container_auto"},
        {
            "type": "container_auto",
            "network_policy": {"type": "disabled"},
        },
        {"type": "container_reference", "container_id": "cntr_abc"},
    ],
    ids=["container_auto", "container_auto_disabled_policy", "container_reference"],
)
def test_hosted_shell_environment_gets_no_executor(
    environment: ShellToolEnvironment,
):
    """A hosted environment runs on OpenAI's side, and rejects an executor."""
    _payload, received = _activity_input_payload_and_tool(
        ShellTool(environment=environment)
    )

    built = _build_tool(received)

    assert isinstance(built, ShellTool)
    assert built.executor is None
    assert _fields(built.environment) == environment


def test_code_interpreter_container_id_is_passed_through():
    """A container named by ID carries no network policy, so it has no secrets."""
    _payload, received = _activity_input_payload_and_tool(
        CodeInterpreterTool(
            tool_config={"type": "code_interpreter", "container": "cntr_abc"}
        )
    )

    built = _build_tool(received)

    assert isinstance(built, CodeInterpreterTool)
    assert _fields(built.tool_config)["container"] == "cntr_abc"


@pytest.mark.parametrize(
    "container",
    [
        {"type": "auto"},
        {"type": "auto", "network_policy": {"type": "disabled"}},
    ],
    ids=["no_policy", "disabled_policy"],
)
def test_code_interpreter_container_without_domain_secrets_is_passed_through(
    container: Any,
):
    """Only a policy carries domain secrets, and only an allowlist has any."""
    _payload, received = _activity_input_payload_and_tool(
        CodeInterpreterTool(
            tool_config={"type": "code_interpreter", "container": container}
        )
    )

    built = _build_tool(received)

    assert isinstance(built, CodeInterpreterTool)
    assert _fields(built.tool_config)["container"] == container


@pytest.mark.parametrize("value", ["", None])
def test_unset_or_empty_environment_variable_is_not_retryable(
    monkeypatch: pytest.MonkeyPatch, value: str | None
):
    if value is None:
        monkeypatch.delenv(ENV_KEY, raising=False)
    else:
        monkeypatch.setenv(ENV_KEY, value)
    marker = secret_reference(ENV_KEY)
    _payload, received = _activity_input_payload_and_tool(
        _hosted_mcp_tool(marker, marker)
    )

    with pytest.raises(ApplicationError) as err:
        _build_tool(received)

    assert err.value.non_retryable
    assert err.value.type == "SecretReferenceFailure"
    assert ENV_KEY in err.value.message


def test_marker_without_a_variable_name_is_rejected_as_malformed():
    """The marker format is public, so a hand-written one can name nothing."""
    _payload, received = _activity_input_payload_and_tool(
        _hosted_mcp_tool("temporal.secret_reference:", "plain")
    )

    with pytest.raises(ApplicationError) as err:
        _build_tool(received)

    assert err.value.non_retryable
    assert err.value.type == "SecretReferenceFailure"
    assert "Malformed secret reference" in err.value.message


@pytest.mark.parametrize(
    ("entry", "type_name"),
    [
        ({"domain": "example.com", "name": "TOKEN", "vaule": SENTINEL}, "dict"),
        ({"domain": "example.com", "name": "TOKEN", "value": 7}, "int"),
        (None, "NoneType"),
        (42, "int"),
        (SENTINEL, "str"),
    ],
    ids=["mis_keyed", "wrong_value_type", "null", "number", "bare_string"],
)
def test_a_malformed_domain_secret_is_rejected_non_retryably(
    entry: Any, type_name: str
):
    """The rejection reports a position and a type, and nothing else.

    Neither the message nor the failure a worker reports may carry what
    pydantic rejected, which for some shapes is the whole entry.
    """
    _payload, received = _activity_input_payload_and_tool(_code_interpreter_tool(entry))

    with pytest.raises(ApplicationError) as err:
        _build_tool(received)

    assert err.value.non_retryable
    assert err.value.type == "SecretReferenceFailure"
    assert f"the type of the value that was rejected ({type_name})" in err.value.message
    assert SENTINEL not in err.value.message
    failure = _reported_failure(err.value)
    assert not failure.HasField("cause")
    assert SENTINEL.encode() not in failure.SerializeToString()


def test_a_malformed_domain_secret_is_reported_by_position(
    monkeypatch: pytest.MonkeyPatch,
):
    """A policy can carry several secrets, so the rejection says which one."""
    monkeypatch.setenv(ENV_KEY, SENTINEL)
    _payload, received = _activity_input_payload_and_tool(
        _code_interpreter_tool(_domain_secret("TOKEN", secret_reference(ENV_KEY)), 42)
    )

    with pytest.raises(ApplicationError) as err:
        _build_tool(received)

    assert "Domain secret 1" in err.value.message
    assert SENTINEL not in err.value.message


def test_a_malformed_domain_secret_is_rejected_again_on_a_second_build():
    """A failed read consumes the secrets, so a second build has to fail too."""
    _payload, received = _activity_input_payload_and_tool(_code_interpreter_tool(42))

    with pytest.raises(ApplicationError):
        _build_tool(received)
    with pytest.raises(ApplicationError) as err:
        _build_tool(received)

    assert err.value.non_retryable
    assert err.value.type == "SecretReferenceFailure"


def test_a_policy_with_no_domain_secrets_is_passed_through():
    """No domain secrets is no secrets to resolve, and nothing to materialize."""
    policy: Any = {"type": "allowlist", "allowed_domains": ["example.com"]}
    _payload, received = _activity_input_payload_and_tool(
        CodeInterpreterTool(
            tool_config={
                "type": "code_interpreter",
                "container": {"type": "auto", "network_policy": policy},
            }
        )
    )

    built = _build_tool(received)

    assert isinstance(built, CodeInterpreterTool)
    assert _fields(built.tool_config)["container"] == {
        "type": "auto",
        "network_policy": policy,
    }


def test_plain_values_are_passed_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_KEY, SENTINEL)
    plain = "temporal.secret_reference-but-not-quite"
    _payload, received = _activity_input_payload_and_tool(
        _hosted_mcp_tool(plain, plain)
    )

    built = _build_tool(received)

    assert isinstance(built, HostedMCPTool)
    assert _fields(built.tool_config)["authorization"] == plain
    assert _fields(built.tool_config)["headers"] == {
        "X-Token": plain,
        "X-Plain": "not-a-secret",
    }


def test_secret_reference_rejects_an_empty_key():
    with pytest.raises(AgentsWorkflowError):
        secret_reference("")


async def _no_stream_events() -> AsyncIterator[TResponseStreamEvent]:
    """A stream that completes without events.

    Publishing one costs a real wait: the activity signals it to a workflow that
    does not exist, and the flusher then retries for ten minutes.
    """
    events: list[TResponseStreamEvent] = []
    for event in events:
        yield event


class _ToolRecordingModel(Model):
    """Records the tools the model activity hands the model."""

    def __init__(self) -> None:
        self.called = False
        self.tools: list[Tool] = []

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        **kwargs: Any,
    ) -> ModelResponse:
        """Record the tools, and answer with nothing."""
        self.called = True
        self.tools = tools
        return ModelResponse(output=[], usage=Usage(), response_id=None)

    def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        **kwargs: Any,
    ) -> AsyncIterator[TResponseStreamEvent]:
        """Record the tools, and stream nothing."""
        self.called = True
        self.tools = tools
        return _no_stream_events()


def _hosted_mcp_config_the_model_received(model: _ToolRecordingModel) -> dict[str, Any]:
    """The tool config of the single hosted MCP tool the model was handed."""
    assert len(model.tools) == 1
    tool = model.tools[0]
    assert isinstance(tool, HostedMCPTool)
    return _fields(tool.tool_config)


async def test_invoke_model_activity_resolves_tool_secrets(
    monkeypatch: pytest.MonkeyPatch,
):
    """The model is handed the secret, not the marker the workflow sent."""
    monkeypatch.setenv(ENV_KEY, SENTINEL)
    sent = _hosted_mcp_tool(secret_reference(ENV_KEY), "not-a-secret")
    _payload, activity_input = _round_trip_activity_input(sent)
    model = _ToolRecordingModel()

    await ActivityEnvironment().run(
        ModelActivity(TestModelProvider(model)).invoke_model_activity,
        activity_input,
    )

    assert _hosted_mcp_config_the_model_received(model) == {
        **_fields(sent.tool_config),
        "authorization": SENTINEL,
    }


async def test_invoke_model_activity_rejects_a_malformed_domain_secret():
    """A rejection reaches the caller as the activity's own failure.

    A retryable escape would run the attempt again, with the same argument,
    forever.
    """
    _payload, activity_input = _round_trip_activity_input(
        _code_interpreter_tool(
            {"domain": "example.com", "name": "TOKEN", "vaule": SENTINEL}
        )
    )
    model = _ToolRecordingModel()

    with pytest.raises(ApplicationError) as err:
        await ActivityEnvironment().run(
            ModelActivity(TestModelProvider(model)).invoke_model_activity,
            activity_input,
        )

    assert err.value.non_retryable
    assert err.value.type == "SecretReferenceFailure"
    assert SENTINEL not in err.value.message
    failure = _reported_failure(err.value)
    assert not failure.HasField("cause")
    assert SENTINEL.encode() not in failure.SerializeToString()
    assert not model.called


async def test_invoke_model_activity_streaming_resolves_tool_secrets(
    monkeypatch: pytest.MonkeyPatch, client: Client
):
    """The streaming activity hands the model the secret by its own path."""
    monkeypatch.setenv(ENV_KEY, SENTINEL)
    sent = _hosted_mcp_tool(secret_reference(ENV_KEY), "not-a-secret")
    _payload, activity_input = _round_trip_activity_input(sent)
    streaming_input: StreamingActivityModelInput = {
        **activity_input,
        "streaming_topic": "events",
    }
    model = _ToolRecordingModel()

    await ActivityEnvironment(client).run(
        ModelActivity(TestModelProvider(model)).invoke_model_activity_streaming,
        streaming_input,
    )

    assert _hosted_mcp_config_the_model_received(model) == {
        **_fields(sent.tool_config),
        "authorization": SENTINEL,
    }


@workflow.defn
class SecretReferenceWorkflow:
    """Builds a hosted MCP tool config the way a user's workflow would."""

    @workflow.run
    async def run(self, key: str) -> str:
        tool_config: Any = {
            "type": "mcp",
            "server_label": "test_server",
            "server_url": "https://example.com/mcp",
            "authorization": secret_reference(key),
        }
        tool = HostedMCPTool(tool_config=tool_config)
        return _fields(tool.tool_config)["authorization"]


async def test_secret_reference_can_be_called_from_workflow_code(client: Client):
    """A marker built in workflow code reaches the workflow result unchanged."""
    async with AgentEnvironment(
        model=TestModel.returning_responses([]),
    ) as env:
        client = env.applied_on_client(client)
        async with new_worker(client, SecretReferenceWorkflow) as worker:
            result = await client.execute_workflow(
                SecretReferenceWorkflow.run,
                ENV_KEY,
                id=f"secret-reference-workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

    assert result == f"temporal.secret_reference:{ENV_KEY}"
