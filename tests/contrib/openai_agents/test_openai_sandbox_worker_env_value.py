from __future__ import annotations

import uuid
from typing import Any, Literal

import pytest
from agents.sandbox import Manifest
from agents.sandbox.manifest import EnvEntry, Environment, EnvValue, StrEnvValue
from agents.sandbox.session.sandbox_session_state import SandboxSessionState
from agents.sandbox.snapshot import NoopSnapshot
from agents.sandbox.workspace_paths import SandboxPathGrant
from pydantic import BaseModel, TypeAdapter
from pydantic_core import SchemaSerializer
from pydantic_core.core_schema import any_schema

from temporalio.contrib.openai_agents import (
    AllowAllWorkerEnvVars,
    OpenAIPayloadConverter,
    TemporalWorkerEnvValue,
)
from temporalio.contrib.openai_agents.sandbox._temporal_activity_models import ExecArgs
from temporalio.contrib.openai_agents.sandbox._temporal_worker_env_value import (
    _resolvable_worker_env_vars_scope,
)
from temporalio.exceptions import ApplicationError

SECRET = "sk-not-in-history-1234567890"
NAME = "TEST_WORKER_ENV_VALUE_NAME"


class _EnvValueSessionState(SandboxSessionState):
    type: Literal["env_value_test"] = "env_value_test"  # type: ignore[assignment]


def _payload_bytes(value: BaseModel) -> bytes:
    payload = OpenAIPayloadConverter().to_payload(value)
    assert payload is not None
    return payload.data


def _round_trip(value: BaseModel, type_hint: type) -> Any:
    converter = OpenAIPayloadConverter()
    payload = converter.to_payload(value)
    assert payload is not None
    return converter.from_payload(payload, type_hint)


def _manifest(env: dict[str, Any]) -> Manifest:
    return Manifest(environment=Environment(value=env))


def _state(manifest: Manifest) -> _EnvValueSessionState:
    return _EnvValueSessionState(
        manifest=manifest, snapshot=NoopSnapshot(id=str(uuid.uuid4()))
    )


def test_literal_env_value_is_written_into_the_payload() -> None:
    raw = _payload_bytes(_manifest({NAME: SECRET}))
    assert SECRET.encode() in raw


def test_worker_env_value_round_trips_without_the_value() -> None:
    raw = _payload_bytes(_manifest({NAME: TemporalWorkerEnvValue(name=NAME)}))
    assert SECRET.encode() not in raw
    assert b"temporal.worker_env_value" in raw

    back = _round_trip(_manifest({NAME: TemporalWorkerEnvValue(name=NAME)}), Manifest)
    value = back.environment.value[NAME]
    assert isinstance(value, TemporalWorkerEnvValue)
    assert value.name == NAME


def test_worker_env_value_round_trips_inside_an_env_entry() -> None:
    manifest = _manifest({NAME: EnvEntry(value=TemporalWorkerEnvValue(name=NAME))})
    raw = _payload_bytes(manifest)
    assert SECRET.encode() not in raw
    assert b"temporal.worker_env_value" in raw

    back = _round_trip(manifest, Manifest)
    entry = back.environment.value[NAME]
    assert isinstance(entry, EnvEntry)
    assert isinstance(entry.value, TemporalWorkerEnvValue)
    assert entry.value.name == NAME


def test_worker_env_value_survives_the_durable_activity_path() -> None:
    args = ExecArgs(
        state=_state(_manifest({NAME: TemporalWorkerEnvValue(name=NAME)})),
        command=["ls"],
    )
    raw = _payload_bytes(args)
    assert SECRET.encode() not in raw

    back = _round_trip(args, ExecArgs)
    value = back.state.manifest.environment.value[NAME]
    assert isinstance(value, TemporalWorkerEnvValue)
    assert value.name == NAME


def test_literal_env_values_are_untouched_alongside_a_worker_env_value() -> None:
    manifest = _manifest(
        {NAME: TemporalWorkerEnvValue(name=NAME), "REGION": "us-west-2"}
    )
    back = _round_trip(manifest, Manifest)

    assert isinstance(back.environment.value[NAME], TemporalWorkerEnvValue)
    assert back.environment.value["REGION"] == "us-west-2"

    normalized = back.environment.normalized()
    assert isinstance(normalized["REGION"].value, StrEnvValue)
    assert normalized["REGION"].value.value == "us-west-2"


def test_discriminator_survives_exclude_unset() -> None:
    """``type`` is a class default, so ``exclude_unset=True`` must not drop it."""
    serializer = SchemaSerializer(any_schema())
    raw = serializer.to_json(
        _manifest({NAME: TemporalWorkerEnvValue(name=NAME)}), exclude_unset=True
    )
    assert b"temporal.worker_env_value" in raw

    back = TypeAdapter(Manifest).validate_json(raw)
    assert isinstance(back.environment.value[NAME], TemporalWorkerEnvValue)


def test_a_host_path_grant_would_reach_the_payload_unprotected() -> None:
    """Why host-path grants are refused: nothing keeps the host path out of history."""
    manifest = Manifest(
        extra_path_grants=(
            SandboxPathGrant(path="/workspace/shared", host_path="/host/private-dir"),
        )
    )
    assert b"/host/private-dir" in _payload_bytes(manifest)
    assert b"/host/private-dir" in _payload_bytes(_state(manifest))


def test_worker_env_value_tag_is_namespaced() -> None:
    """Upstream raises on a duplicate tag, so the namespace keeps it registrable."""
    tag = TemporalWorkerEnvValue(name=NAME).type
    assert tag.startswith("temporal.")
    assert EnvValue._subclass_registry[tag] is TemporalWorkerEnvValue


async def test_resolve_reads_the_worker_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NAME, SECRET)
    with _resolvable_worker_env_vars_scope([NAME]):
        assert await TemporalWorkerEnvValue(name=NAME).resolve() == SECRET


async def test_resolve_raises_naming_the_variable_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(NAME, raising=False)
    with pytest.raises(ApplicationError) as exc_info:
        with _resolvable_worker_env_vars_scope([NAME]):
            await TemporalWorkerEnvValue(name=NAME).resolve()

    assert NAME in str(exc_info.value)
    # An allowed-but-unset variable must not read as a denial.
    assert "resolvable_worker_env_vars" not in str(exc_info.value)
    assert exc_info.value.type == "TemporalWorkerEnvValueUnresolved"
    assert exc_info.value.non_retryable


async def test_resolve_raises_when_the_variable_is_set_but_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NAME, "")
    with pytest.raises(ApplicationError) as exc_info:
        with _resolvable_worker_env_vars_scope([NAME]):
            await TemporalWorkerEnvValue(name=NAME).resolve()

    assert NAME in str(exc_info.value)
    assert exc_info.value.type == "TemporalWorkerEnvValueUnresolved"
    assert exc_info.value.non_retryable


async def test_resolve_refuses_to_run_on_the_workflow_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NAME, SECRET)
    monkeypatch.setattr("temporalio.workflow.in_workflow", lambda: True)
    with pytest.raises(ApplicationError) as exc_info:
        with _resolvable_worker_env_vars_scope([NAME]):
            await TemporalWorkerEnvValue(name=NAME).resolve()

    assert exc_info.value.non_retryable
    assert SECRET not in str(exc_info.value)


async def test_resolve_refuses_a_name_the_worker_does_not_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NAME, SECRET)
    with pytest.raises(ApplicationError) as exc_info:
        with _resolvable_worker_env_vars_scope(["SOMETHING_ELSE"]):
            await TemporalWorkerEnvValue(name=NAME).resolve()

    assert NAME in str(exc_info.value)
    assert "resolvable_worker_env_vars" in str(exc_info.value)
    assert SECRET not in str(exc_info.value)
    assert exc_info.value.type == "TemporalWorkerEnvValueUnresolved"
    assert exc_info.value.non_retryable


async def test_resolve_refuses_outside_a_sandbox_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NAME, SECRET)
    with pytest.raises(ApplicationError) as exc_info:
        await TemporalWorkerEnvValue(name=NAME).resolve()

    assert "resolvable_worker_env_vars" in str(exc_info.value)


async def test_allow_all_makes_an_unlisted_name_resolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A_NAME_LISTED_NOWHERE", SECRET)
    value = TemporalWorkerEnvValue(name="A_NAME_LISTED_NOWHERE")

    with _resolvable_worker_env_vars_scope(AllowAllWorkerEnvVars()):
        assert await value.resolve() == SECRET


async def test_a_literal_star_in_the_resolvable_names_matches_no_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A_NAME_LISTED_NOWHERE", SECRET)
    value = TemporalWorkerEnvValue(name="A_NAME_LISTED_NOWHERE")

    with pytest.raises(ApplicationError):
        with _resolvable_worker_env_vars_scope(["*"]):
            await value.resolve()


async def test_a_glob_in_the_resolvable_names_matches_no_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NAME, SECRET)
    with pytest.raises(ApplicationError) as exc_info:
        with _resolvable_worker_env_vars_scope(["TEST_WORKER_ENV_VALUE_*"]):
            await TemporalWorkerEnvValue(name=NAME).resolve()

    assert "resolvable_worker_env_vars" in str(exc_info.value)


async def test_each_env_value_resolves_its_own_variable_under_its_own_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_PRIMARY", "primary-secret")
    monkeypatch.setenv("WORKER_SECONDARY", "secondary-secret")
    manifest = _manifest(
        {
            "REGION": "us-west-2",
            "SANDBOX_PRIMARY": TemporalWorkerEnvValue(name="WORKER_PRIMARY"),
            "LOG_LEVEL": "debug",
            "SANDBOX_SECONDARY": TemporalWorkerEnvValue(name="WORKER_SECONDARY"),
        }
    )

    with _resolvable_worker_env_vars_scope(["WORKER_PRIMARY", "WORKER_SECONDARY"]):
        assert await manifest.environment.resolve() == {
            "REGION": "us-west-2",
            "SANDBOX_PRIMARY": "primary-secret",
            "LOG_LEVEL": "debug",
            "SANDBOX_SECONDARY": "secondary-secret",
        }

    raw = _payload_bytes(manifest)
    for secret in (b"primary-secret", b"secondary-secret"):
        assert secret not in raw
    for name in (b"WORKER_PRIMARY", b"WORKER_SECONDARY"):
        assert name in raw


async def test_environment_resolve_leaves_the_manifest_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NAME, SECRET)
    manifest = _manifest(
        {NAME: TemporalWorkerEnvValue(name=NAME), "REGION": "us-west-2"}
    )

    with _resolvable_worker_env_vars_scope([NAME]):
        assert await manifest.environment.resolve() == {
            NAME: SECRET,
            "REGION": "us-west-2",
        }

    assert isinstance(manifest.environment.value[NAME], TemporalWorkerEnvValue)
    assert SECRET.encode() not in _payload_bytes(manifest)
