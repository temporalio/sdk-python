"""Tests for worker environment references in hosted tool secrets."""

import time
import uuid
from collections.abc import AsyncIterator, Collection
from typing import Any, cast

import pytest
from agents import (
    Agent,
    AgentOutputSchemaBase,
    CodeInterpreterTool,
    Handoff,
    HostedMCPTool,
    Model,
    ModelResponse,
    ModelSettings,
    ModelTracing,
    Runner,
    Tool,
    TResponseInputItem,
    Usage,
)
from agents.items import TResponseStreamEvent
from agents.tool import ShellTool, ShellToolEnvironment

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.openai_agents import (
    ModelActivityParameters,
    OpenAIAgentsPlugin,
    OpenAIPayloadConverter,
    temporal_worker_env_ref,
)
from temporalio.contrib.openai_agents._invoke_model_activity import (
    ActivityModelInput,
    ModelActivity,
    StreamingActivityModelInput,
    _build_tool,
)
from temporalio.contrib.openai_agents._temporal_model_stub import _TemporalModelStub
from temporalio.contrib.openai_agents._temporal_worker_env_ref import (
    _WorkerEnvRefResolver,
)
from temporalio.contrib.openai_agents.testing import (
    AgentEnvironment,
    TestModelProvider,
)
from temporalio.testing import ActivityEnvironment
from tests.helpers import new_worker

SENTINEL = "sk-test-sentinel-4f1a9c7e2b"
ENV_NAME = "TEMPORAL_TEST_TOOL_SECRET"
OTHER_SENTINEL = "sk-test-other-8c3d5e0a1f"
OTHER_ENV_NAME = "TEMPORAL_TEST_OTHER_TOOL_SECRET"

_RESOLVER_ALLOWING_TEST_NAMES = _WorkerEnvRefResolver([ENV_NAME, OTHER_ENV_NAME])


def _round_trip_activity_input(tool: Tool) -> tuple[bytes, ActivityModelInput]:
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


def _network_policy(domain_secrets: tuple[Any, ...]) -> Any:
    return {
        "type": "allowlist",
        "allowed_domains": ["example.com"],
        "domain_secrets": list(domain_secrets),
    }


def _shell_tool(*domain_secrets: Any) -> ShellTool:
    environment: Any = {
        "type": "container_auto",
        "network_policy": _network_policy(domain_secrets),
    }
    return ShellTool(environment=environment)


def _code_interpreter_tool(*domain_secrets: Any) -> CodeInterpreterTool:
    tool_config: Any = {
        "type": "code_interpreter",
        "container": {
            "type": "auto",
            "network_policy": _network_policy(domain_secrets),
        },
    }
    return CodeInterpreterTool(tool_config=tool_config)


def _as_dict(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value)


def _secrets_in(network_policy: Any) -> list[Any]:
    return list(_as_dict(network_policy)["domain_secrets"])


def _shell_secrets(built: Tool) -> list[Any]:
    assert isinstance(built, ShellTool)
    assert built.environment is not None
    return _secrets_in(_as_dict(built.environment)["network_policy"])


def _code_interpreter_secrets(built: Tool) -> list[Any]:
    assert isinstance(built, CodeInterpreterTool)
    container = _as_dict(built.tool_config)["container"]
    return _secrets_in(_as_dict(container)["network_policy"])


def test_hosted_mcp_secrets_stay_out_of_activity_arguments(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    ref = temporal_worker_env_ref(ENV_NAME)

    payload, received = _activity_input_payload_and_tool(_hosted_mcp_tool(ref, ref))

    assert SENTINEL.encode() not in payload
    assert payload.count(ref.encode()) == 2
    assert received.tool_config["authorization"] == ref
    assert received.tool_config["headers"]["X-Token"] == ref


def test_hosted_shell_domain_secret_stays_out_of_activity_arguments(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    ref = temporal_worker_env_ref(ENV_NAME)

    payload, received = _activity_input_payload_and_tool(
        _shell_tool(_domain_secret("TOKEN", ref))
    )

    assert SENTINEL.encode() not in payload
    assert ref.encode() in payload
    secrets = _secrets_in(received.environment["network_policy"])
    assert secrets[0]["value"] == ref


def test_code_interpreter_domain_secret_stays_out_of_activity_arguments(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    ref = temporal_worker_env_ref(ENV_NAME)

    payload, received = _activity_input_payload_and_tool(
        _code_interpreter_tool(_domain_secret("TOKEN", ref))
    )

    assert SENTINEL.encode() not in payload
    assert ref.encode() in payload
    secrets = _secrets_in(received.tool_config["container"]["network_policy"])
    assert secrets[0]["value"] == ref


def test_hosted_mcp_secrets_resolve_for_the_model_call(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    ref = temporal_worker_env_ref(ENV_NAME)
    _payload, received = _activity_input_payload_and_tool(_hosted_mcp_tool(ref, ref))

    built = _build_tool(received, _RESOLVER_ALLOWING_TEST_NAMES)

    assert isinstance(built, HostedMCPTool)
    assert _as_dict(built.tool_config)["authorization"] == SENTINEL
    assert _as_dict(built.tool_config)["headers"] == {
        "X-Token": SENTINEL,
        "X-Plain": "not-a-secret",
    }
    assert received.tool_config["authorization"] == ref
    assert received.tool_config["headers"]["X-Token"] == ref


@pytest.mark.parametrize(
    ("resolvable", "resolves"),
    [([ENV_NAME], True), (["*"], True), ([OTHER_ENV_NAME], False)],
    ids=["the_name", "star", "another_name"],
)
def test_a_reference_resolves_only_from_a_variable_the_worker_allows(
    monkeypatch: pytest.MonkeyPatch, resolvable: Collection[str], resolves: bool
):
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    ref = temporal_worker_env_ref(ENV_NAME)
    _payload, received = _activity_input_payload_and_tool(_hosted_mcp_tool(ref, ref))

    built = _build_tool(received, _WorkerEnvRefResolver(resolvable))

    assert isinstance(built, HostedMCPTool)
    expected = SENTINEL if resolves else ref
    assert _as_dict(built.tool_config)["authorization"] == expected
    assert _as_dict(built.tool_config)["headers"]["X-Token"] == expected


def test_star_anywhere_in_the_resolvable_names_resolves_every_name(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    ref = temporal_worker_env_ref(ENV_NAME)
    _payload, received = _activity_input_payload_and_tool(_hosted_mcp_tool(ref, ref))

    built = _build_tool(received, _WorkerEnvRefResolver([OTHER_ENV_NAME, "*"]))

    assert isinstance(built, HostedMCPTool)
    assert _as_dict(built.tool_config)["authorization"] == SENTINEL
    assert _as_dict(built.tool_config)["headers"]["X-Token"] == SENTINEL


def test_a_glob_in_the_resolvable_names_matches_no_name(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    ref = temporal_worker_env_ref(ENV_NAME)
    _payload, received = _activity_input_payload_and_tool(_hosted_mcp_tool(ref, ref))

    built = _build_tool(received, _WorkerEnvRefResolver(["TEMPORAL_TEST_*"]))

    assert isinstance(built, HostedMCPTool)
    assert _as_dict(built.tool_config)["authorization"] == ref
    assert _as_dict(built.tool_config)["headers"]["X-Token"] == ref


def test_hosted_shell_domain_secret_resolves_for_the_model_call(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    ref = temporal_worker_env_ref(ENV_NAME)
    _payload, received = _activity_input_payload_and_tool(
        _shell_tool(_domain_secret("TOKEN", ref))
    )

    built = _build_tool(received, _RESOLVER_ALLOWING_TEST_NAMES)

    assert _shell_secrets(built) == [_domain_secret("TOKEN", SENTINEL)]


def test_code_interpreter_domain_secret_resolves_for_the_model_call(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    ref = temporal_worker_env_ref(ENV_NAME)
    _payload, received = _activity_input_payload_and_tool(
        _code_interpreter_tool(_domain_secret("TOKEN", ref))
    )

    built = _build_tool(received, _RESOLVER_ALLOWING_TEST_NAMES)

    assert _code_interpreter_secrets(built) == [_domain_secret("TOKEN", SENTINEL)]


def test_code_interpreter_domain_secret_survives_a_second_build(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    ref = temporal_worker_env_ref(ENV_NAME)
    _payload, received = _activity_input_payload_and_tool(
        _code_interpreter_tool(_domain_secret("TOKEN", ref))
    )

    first = _build_tool(received, _RESOLVER_ALLOWING_TEST_NAMES)
    second = _build_tool(received, _RESOLVER_ALLOWING_TEST_NAMES)

    assert _code_interpreter_secrets(first) == [_domain_secret("TOKEN", SENTINEL)]
    assert _code_interpreter_secrets(second) == [_domain_secret("TOKEN", SENTINEL)]
    assert _secrets_in(received.tool_config["container"]["network_policy"]) == [
        _domain_secret("TOKEN", ref)
    ]


def test_only_domain_secrets_holding_a_worker_env_ref_are_resolved(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    ref = temporal_worker_env_ref(ENV_NAME)
    literal = _domain_secret("PLAIN", "plain-token-value")
    _payload, received = _activity_input_payload_and_tool(
        _code_interpreter_tool(literal, _domain_secret("TOKEN", ref))
    )

    built = _build_tool(received, _RESOLVER_ALLOWING_TEST_NAMES)

    assert _code_interpreter_secrets(built) == [
        literal,
        _domain_secret("TOKEN", SENTINEL),
    ]


def test_two_domain_secrets_resolve_to_their_own_secrets(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    monkeypatch.setenv(OTHER_ENV_NAME, OTHER_SENTINEL)
    _payload, received = _activity_input_payload_and_tool(
        _code_interpreter_tool(
            _domain_secret("TOKEN", temporal_worker_env_ref(ENV_NAME)),
            _domain_secret("OTHER", temporal_worker_env_ref(OTHER_ENV_NAME)),
        )
    )

    built = _build_tool(received, _RESOLVER_ALLOWING_TEST_NAMES)

    assert _code_interpreter_secrets(built) == [
        _domain_secret("TOKEN", SENTINEL),
        _domain_secret("OTHER", OTHER_SENTINEL),
    ]


def test_shell_domain_secrets_resolve_to_their_own_secrets_in_order(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    monkeypatch.setenv(OTHER_ENV_NAME, OTHER_SENTINEL)
    literal = _domain_secret("PLAIN", "plain-token-value")
    _payload, received = _activity_input_payload_and_tool(
        _shell_tool(
            literal,
            _domain_secret("TOKEN", temporal_worker_env_ref(ENV_NAME)),
            _domain_secret("OTHER", temporal_worker_env_ref(OTHER_ENV_NAME)),
        )
    )

    built = _build_tool(received, _RESOLVER_ALLOWING_TEST_NAMES)

    assert _shell_secrets(built) == [
        literal,
        _domain_secret("TOKEN", SENTINEL),
        _domain_secret("OTHER", OTHER_SENTINEL),
    ]


def test_a_shell_domain_secret_naming_a_denied_variable_is_passed_through(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(OTHER_ENV_NAME, OTHER_SENTINEL)
    denied = temporal_worker_env_ref(OTHER_ENV_NAME)
    _payload, received = _activity_input_payload_and_tool(
        _shell_tool(_domain_secret("TOKEN", denied))
    )

    built = _build_tool(received, _WorkerEnvRefResolver([ENV_NAME]))

    assert _shell_secrets(built) == [_domain_secret("TOKEN", denied)]


def test_a_code_interpreter_domain_secret_naming_a_denied_variable_is_passed_through(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(OTHER_ENV_NAME, OTHER_SENTINEL)
    denied = temporal_worker_env_ref(OTHER_ENV_NAME)
    _payload, received = _activity_input_payload_and_tool(
        _code_interpreter_tool(_domain_secret("TOKEN", denied))
    )

    built = _build_tool(received, _WorkerEnvRefResolver([ENV_NAME]))

    assert _code_interpreter_secrets(built) == [_domain_secret("TOKEN", denied)]


def test_two_worker_env_refs_in_one_mcp_config_resolve_to_their_own_secrets(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    monkeypatch.setenv(OTHER_ENV_NAME, OTHER_SENTINEL)
    _payload, received = _activity_input_payload_and_tool(
        _hosted_mcp_tool(
            temporal_worker_env_ref(ENV_NAME), temporal_worker_env_ref(OTHER_ENV_NAME)
        )
    )

    built = _build_tool(received, _RESOLVER_ALLOWING_TEST_NAMES)

    assert isinstance(built, HostedMCPTool)
    assert _as_dict(built.tool_config)["authorization"] == SENTINEL
    assert _as_dict(built.tool_config)["headers"] == {
        "X-Token": OTHER_SENTINEL,
        "X-Plain": "not-a-secret",
    }


def test_a_worker_env_ref_in_a_header_name_is_passed_through(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    ref = temporal_worker_env_ref(ENV_NAME)
    tool_config: Any = {
        "type": "mcp",
        "server_label": "test_server",
        "server_url": "https://example.com/mcp",
        "headers": {ref: "not-a-secret"},
    }

    payload, received = _activity_input_payload_and_tool(
        HostedMCPTool(tool_config=tool_config)
    )

    built = _build_tool(received, _RESOLVER_ALLOWING_TEST_NAMES)

    assert isinstance(built, HostedMCPTool)
    assert _as_dict(built.tool_config)["headers"] == {ref: "not-a-secret"}
    assert SENTINEL.encode() not in payload


def test_an_mcp_config_without_an_authorization_resolves_its_headers(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    tool_config: Any = {
        "type": "mcp",
        "server_label": "test_server",
        "server_url": "https://example.com/mcp",
        "headers": {"X-Token": temporal_worker_env_ref(ENV_NAME)},
    }

    _payload, received = _activity_input_payload_and_tool(
        HostedMCPTool(tool_config=tool_config)
    )

    built = _build_tool(received, _RESOLVER_ALLOWING_TEST_NAMES)

    assert isinstance(built, HostedMCPTool)
    assert "authorization" not in _as_dict(built.tool_config)
    assert _as_dict(built.tool_config)["headers"] == {"X-Token": SENTINEL}


def test_an_mcp_config_without_headers_resolves_its_authorization(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    tool_config: Any = {
        "type": "mcp",
        "server_label": "test_server",
        "server_url": "https://example.com/mcp",
        "authorization": temporal_worker_env_ref(ENV_NAME),
    }

    _payload, received = _activity_input_payload_and_tool(
        HostedMCPTool(tool_config=tool_config)
    )

    built = _build_tool(received, _RESOLVER_ALLOWING_TEST_NAMES)

    assert isinstance(built, HostedMCPTool)
    assert _as_dict(built.tool_config)["authorization"] == SENTINEL
    assert "headers" not in _as_dict(built.tool_config)


def test_local_shell_environment_keeps_its_executor():
    _payload, received = _activity_input_payload_and_tool(
        ShellTool(environment={"type": "local"}, executor=lambda _request: "")
    )

    built = _build_tool(received, _RESOLVER_ALLOWING_TEST_NAMES)

    assert isinstance(built, ShellTool)
    assert built.executor is not None
    assert _as_dict(built.environment) == {"type": "local"}


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
    _payload, received = _activity_input_payload_and_tool(
        ShellTool(environment=environment)
    )

    built = _build_tool(received, _RESOLVER_ALLOWING_TEST_NAMES)

    assert isinstance(built, ShellTool)
    assert built.executor is None
    assert _as_dict(built.environment) == environment


def test_code_interpreter_container_id_is_passed_through():
    _payload, received = _activity_input_payload_and_tool(
        CodeInterpreterTool(
            tool_config={"type": "code_interpreter", "container": "cntr_abc"}
        )
    )

    built = _build_tool(received, _RESOLVER_ALLOWING_TEST_NAMES)

    assert isinstance(built, CodeInterpreterTool)
    assert _as_dict(built.tool_config)["container"] == "cntr_abc"


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
    _payload, received = _activity_input_payload_and_tool(
        CodeInterpreterTool(
            tool_config={"type": "code_interpreter", "container": container}
        )
    )

    built = _build_tool(received, _RESOLVER_ALLOWING_TEST_NAMES)

    assert isinstance(built, CodeInterpreterTool)
    assert _as_dict(built.tool_config)["container"] == container


def test_an_unset_environment_variable_resolves_to_an_empty_value(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv(ENV_NAME, raising=False)
    ref = temporal_worker_env_ref(ENV_NAME)
    _payload, received = _activity_input_payload_and_tool(_hosted_mcp_tool(ref, ref))

    built = _build_tool(received, _RESOLVER_ALLOWING_TEST_NAMES)

    assert isinstance(built, HostedMCPTool)
    assert _as_dict(built.tool_config)["authorization"] == ""
    assert _as_dict(built.tool_config)["headers"]["X-Token"] == ""


def test_a_policy_with_no_domain_secrets_is_passed_through():
    policy: Any = {"type": "allowlist", "allowed_domains": ["example.com"]}
    _payload, received = _activity_input_payload_and_tool(
        CodeInterpreterTool(
            tool_config={
                "type": "code_interpreter",
                "container": {"type": "auto", "network_policy": policy},
            }
        )
    )

    built = _build_tool(received, _RESOLVER_ALLOWING_TEST_NAMES)

    assert isinstance(built, CodeInterpreterTool)
    assert _as_dict(built.tool_config)["container"] == {
        "type": "auto",
        "network_policy": policy,
    }


def test_plain_values_are_passed_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    plain = "temporal.worker_env_ref-but-not-quite"
    _payload, received = _activity_input_payload_and_tool(
        _hosted_mcp_tool(plain, plain)
    )

    built = _build_tool(received, _RESOLVER_ALLOWING_TEST_NAMES)

    assert isinstance(built, HostedMCPTool)
    assert _as_dict(built.tool_config)["authorization"] == plain
    assert _as_dict(built.tool_config)["headers"] == {
        "X-Token": plain,
        "X-Plain": "not-a-secret",
    }


def test_a_worker_env_ref_inside_a_larger_value_is_substituted_in_place(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    composed = "Bearer " + temporal_worker_env_ref(ENV_NAME)
    _payload, received = _activity_input_payload_and_tool(
        _hosted_mcp_tool(composed, composed)
    )

    built = _build_tool(received, _RESOLVER_ALLOWING_TEST_NAMES)

    assert isinstance(built, HostedMCPTool)
    assert _as_dict(built.tool_config)["authorization"] == f"Bearer {SENTINEL}"
    assert _as_dict(built.tool_config)["headers"]["X-Token"] == f"Bearer {SENTINEL}"


def test_one_value_holding_two_refs_resolves_only_the_allowed_name(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    monkeypatch.setenv(OTHER_ENV_NAME, OTHER_SENTINEL)
    denied = temporal_worker_env_ref(OTHER_ENV_NAME)
    composed = f"{temporal_worker_env_ref(ENV_NAME)} {denied}"
    _payload, received = _activity_input_payload_and_tool(
        _hosted_mcp_tool(composed, composed)
    )

    built = _build_tool(received, _WorkerEnvRefResolver([ENV_NAME]))

    assert isinstance(built, HostedMCPTool)
    assert _as_dict(built.tool_config)["authorization"] == f"{SENTINEL} {denied}"
    assert _as_dict(built.tool_config)["headers"]["X-Token"] == f"{SENTINEL} {denied}"


def test_a_worker_env_ref_with_no_closing_brace_is_passed_through(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    truncated = "temporal.worker_env_ref:{" + ENV_NAME
    _payload, received = _activity_input_payload_and_tool(
        _hosted_mcp_tool(truncated, truncated)
    )

    built = _build_tool(received, _WorkerEnvRefResolver(["*"]))

    assert isinstance(built, HostedMCPTool)
    assert _as_dict(built.tool_config)["authorization"] == truncated
    assert _as_dict(built.tool_config)["headers"]["X-Token"] == truncated


def test_a_value_packed_with_unclosed_references_does_not_stall_the_resolver():
    opener = "temporal.worker_env_ref:{"
    packed = opener * (1024 * 1024 // len(opener))
    _payload, received = _activity_input_payload_and_tool(
        _hosted_mcp_tool(packed, "not-a-secret")
    )

    start = time.monotonic()
    built = _build_tool(received, _WorkerEnvRefResolver([]))
    elapsed = time.monotonic() - start

    assert isinstance(built, HostedMCPTool)
    assert _as_dict(built.tool_config)["authorization"] == packed
    assert elapsed < 5.0


def test_a_bare_string_is_rejected_as_the_resolvable_variable_names():
    with pytest.raises(TypeError, match="resolvable_worker_env_vars"):
        _WorkerEnvRefResolver(ENV_NAME)


async def _no_stream_events() -> AsyncIterator[TResponseStreamEvent]:
    """Publishing an event here makes the flusher retry for ten minutes against a workflow that does not exist."""
    events: list[TResponseStreamEvent] = []
    for event in events:
        yield event


class _ToolRecordingModel(Model):
    def __init__(self) -> None:
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
        self.tools = tools
        return _no_stream_events()


def _hosted_mcp_config_the_model_received(model: _ToolRecordingModel) -> dict[str, Any]:
    assert len(model.tools) == 1
    tool = model.tools[0]
    assert isinstance(tool, HostedMCPTool)
    return _as_dict(tool.tool_config)


async def test_invoke_model_activity_resolves_tool_secrets(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    sent = _hosted_mcp_tool(temporal_worker_env_ref(ENV_NAME), "not-a-secret")
    _payload, activity_input = _round_trip_activity_input(sent)
    model = _ToolRecordingModel()

    await ActivityEnvironment().run(
        ModelActivity(
            TestModelProvider(model), resolvable_worker_env_vars=[ENV_NAME]
        ).invoke_model_activity,
        activity_input,
    )

    assert _hosted_mcp_config_the_model_received(model) == {
        **_as_dict(sent.tool_config),
        "authorization": SENTINEL,
    }


async def test_invoke_model_activity_resolves_nothing_by_default(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    sent = _hosted_mcp_tool(temporal_worker_env_ref(ENV_NAME), "not-a-secret")
    _payload, activity_input = _round_trip_activity_input(sent)
    model = _ToolRecordingModel()

    await ActivityEnvironment().run(
        ModelActivity(TestModelProvider(model)).invoke_model_activity,
        activity_input,
    )

    assert _hosted_mcp_config_the_model_received(model) == _as_dict(sent.tool_config)


async def test_invoke_model_activity_streaming_resolves_tool_secrets(
    monkeypatch: pytest.MonkeyPatch, client: Client
):
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    sent = _hosted_mcp_tool(temporal_worker_env_ref(ENV_NAME), "not-a-secret")
    _payload, activity_input = _round_trip_activity_input(sent)
    streaming_input: StreamingActivityModelInput = {
        **activity_input,
        "streaming_topic": "events",
    }
    model = _ToolRecordingModel()

    await ActivityEnvironment(client).run(
        ModelActivity(
            TestModelProvider(model), resolvable_worker_env_vars=[ENV_NAME]
        ).invoke_model_activity_streaming,
        streaming_input,
    )

    assert _hosted_mcp_config_the_model_received(model) == {
        **_as_dict(sent.tool_config),
        "authorization": SENTINEL,
    }


@workflow.defn
class WorkerEnvRefAgentWorkflow:
    @workflow.run
    async def run(self) -> None:
        tool_config: Any = {
            "type": "mcp",
            "server_label": "test_server",
            "server_url": "https://example.com/mcp",
            "authorization": temporal_worker_env_ref(ENV_NAME),
            "headers": {"X-Token": temporal_worker_env_ref(OTHER_ENV_NAME)},
        }
        agent = Agent[None](
            name="Worker env ref agent",
            tools=[HostedMCPTool(tool_config=tool_config)],
        )
        await Runner.run(starting_agent=agent, input="hi")


async def test_a_worker_resolves_only_the_variables_its_plugin_names(
    monkeypatch: pytest.MonkeyPatch, client: Client
):
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    monkeypatch.setenv(OTHER_ENV_NAME, OTHER_SENTINEL)
    model = _ToolRecordingModel()
    config = client.config()
    config["plugins"] = [
        *config.get("plugins", []),
        OpenAIAgentsPlugin(
            model_provider=TestModelProvider(model),
            resolvable_worker_env_vars=[ENV_NAME],
        ),
    ]
    client = Client(**config)

    async with new_worker(client, WorkerEnvRefAgentWorkflow) as worker:
        await client.execute_workflow(
            WorkerEnvRefAgentWorkflow.run,
            id=f"worker-env-ref-allowlist-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

    tool_config = _hosted_mcp_config_the_model_received(model)
    assert tool_config["authorization"] == SENTINEL
    assert tool_config["headers"] == {
        "X-Token": temporal_worker_env_ref(OTHER_ENV_NAME)
    }


async def test_an_agent_environment_forwards_the_variables_it_names_to_the_worker(
    monkeypatch: pytest.MonkeyPatch, client: Client
):
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    monkeypatch.setenv(OTHER_ENV_NAME, OTHER_SENTINEL)
    model = _ToolRecordingModel()

    async with AgentEnvironment(
        model=model, resolvable_worker_env_vars=[ENV_NAME]
    ) as env:
        client = env.applied_on_client(client)
        async with new_worker(client, WorkerEnvRefAgentWorkflow) as worker:
            await client.execute_workflow(
                WorkerEnvRefAgentWorkflow.run,
                id=f"agent-environment-env-ref-allowlist-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

    tool_config = _hosted_mcp_config_the_model_received(model)
    assert tool_config["authorization"] == SENTINEL
    assert tool_config["headers"] == {
        "X-Token": temporal_worker_env_ref(OTHER_ENV_NAME)
    }
