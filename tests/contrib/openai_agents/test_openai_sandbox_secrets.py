"""Tests for keeping sandbox environment secrets out of workflow history."""

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

from temporalio.contrib.openai_agents import OpenAIPayloadConverter, SecretRef
from temporalio.contrib.openai_agents.sandbox._temporal_activity_models import ExecArgs
from temporalio.exceptions import ApplicationError

SECRET = "sk-not-in-history-1234567890"
KEY = "TEST_SECRET_REF_KEY"


class _SecretRefSessionState(SandboxSessionState):
    """Concrete session state so manifests can travel the real activity models."""

    type: Literal["secret_ref_test"] = "secret_ref_test"  # type: ignore[assignment]


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


def _state(manifest: Manifest) -> _SecretRefSessionState:
    return _SecretRefSessionState(
        manifest=manifest, snapshot=NoopSnapshot(id=str(uuid.uuid4()))
    )


def test_literal_env_value_is_written_into_the_payload() -> None:
    """The motivating behaviour, not a defect: literal values keep working."""
    raw = _payload_bytes(_manifest({KEY: SECRET}))
    assert SECRET.encode() in raw


def test_secret_ref_round_trips_without_the_secret() -> None:
    raw = _payload_bytes(_manifest({KEY: SecretRef(key=KEY)}))
    assert SECRET.encode() not in raw
    assert b"temporal.secret_ref" in raw

    back = _round_trip(_manifest({KEY: SecretRef(key=KEY)}), Manifest)
    value = back.environment.value[KEY]
    assert isinstance(value, SecretRef)
    assert value.key == KEY


def test_secret_ref_round_trips_inside_an_env_entry() -> None:
    manifest = _manifest({KEY: EnvEntry(value=SecretRef(key=KEY))})
    raw = _payload_bytes(manifest)
    assert SECRET.encode() not in raw
    assert b"temporal.secret_ref" in raw

    back = _round_trip(manifest, Manifest)
    entry = back.environment.value[KEY]
    assert isinstance(entry, EnvEntry)
    assert isinstance(entry.value, SecretRef)
    assert entry.value.key == KEY


def test_secret_ref_survives_the_durable_activity_path() -> None:
    args = ExecArgs(state=_state(_manifest({KEY: SecretRef(key=KEY)})), command=["ls"])
    raw = _payload_bytes(args)
    assert SECRET.encode() not in raw

    back = _round_trip(args, ExecArgs)
    value = back.state.manifest.environment.value[KEY]
    assert isinstance(value, SecretRef)
    assert value.key == KEY


def test_literal_env_values_are_untouched_alongside_a_secret_ref() -> None:
    manifest = _manifest({KEY: SecretRef(key=KEY), "REGION": "us-west-2"})
    back = _round_trip(manifest, Manifest)

    assert isinstance(back.environment.value[KEY], SecretRef)
    assert back.environment.value["REGION"] == "us-west-2"

    normalized = back.environment.normalized()
    assert isinstance(normalized["REGION"].value, StrEnvValue)
    assert normalized["REGION"].value.value == "us-west-2"


def test_discriminator_survives_exclude_unset() -> None:
    """Guards a property split between our ``exclude_unset`` and upstream's wrap serializers."""
    serializer = SchemaSerializer(any_schema())
    raw = serializer.to_json(_manifest({KEY: SecretRef(key=KEY)}), exclude_unset=True)
    assert b"temporal.secret_ref" in raw

    back = TypeAdapter(Manifest).validate_json(raw)
    assert isinstance(back.environment.value[KEY], SecretRef)


def test_a_host_path_grant_would_reach_the_payload_unprotected() -> None:
    """Why host-path grants are refused: nothing keeps the host path out of history."""
    manifest = Manifest(
        extra_path_grants=(
            SandboxPathGrant(path="/workspace/shared", host_path="/host/private-dir"),
        )
    )
    assert b"/host/private-dir" in _payload_bytes(manifest)
    assert b"/host/private-dir" in _payload_bytes(_state(manifest))


def test_secret_ref_tag_is_namespaced() -> None:
    """Upstream raises on a duplicate tag, so the namespace keeps it registrable."""
    tag = SecretRef(key=KEY).type
    assert tag.startswith("temporal.")
    assert EnvValue._subclass_registry[tag] is SecretRef


# ── resolve() ──


async def test_resolve_reads_the_worker_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(KEY, SECRET)
    assert await SecretRef(key=KEY).resolve() == SECRET


async def test_resolve_raises_naming_the_key_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(KEY, raising=False)
    with pytest.raises(ApplicationError) as exc_info:
        await SecretRef(key=KEY).resolve()

    assert KEY in str(exc_info.value)
    assert exc_info.value.non_retryable


async def test_resolve_raises_when_the_variable_is_set_but_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(KEY, "")
    with pytest.raises(ApplicationError) as exc_info:
        await SecretRef(key=KEY).resolve()

    assert KEY in str(exc_info.value)
    assert exc_info.value.non_retryable


async def test_resolve_refuses_to_run_on_the_workflow_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(KEY, SECRET)
    monkeypatch.setattr("temporalio.workflow.in_workflow", lambda: True)
    with pytest.raises(ApplicationError) as exc_info:
        await SecretRef(key=KEY).resolve()

    assert exc_info.value.non_retryable
    assert SECRET not in str(exc_info.value)


async def test_each_reference_resolves_its_own_variable_under_its_own_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crossed names pin key-to-value pairing, which resolving by mapping key breaks."""
    monkeypatch.setenv("WORKER_PRIMARY", "primary-secret")
    monkeypatch.setenv("WORKER_SECONDARY", "secondary-secret")
    manifest = _manifest(
        {
            "REGION": "us-west-2",
            "SANDBOX_PRIMARY": SecretRef(key="WORKER_PRIMARY"),
            "LOG_LEVEL": "debug",
            "SANDBOX_SECONDARY": SecretRef(key="WORKER_SECONDARY"),
        }
    )

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


async def test_environment_resolve_leaves_the_manifest_holding_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(KEY, SECRET)
    manifest = _manifest({KEY: SecretRef(key=KEY), "REGION": "us-west-2"})

    assert await manifest.environment.resolve() == {KEY: SECRET, "REGION": "us-west-2"}

    assert isinstance(manifest.environment.value[KEY], SecretRef)
    assert SECRET.encode() not in _payload_bytes(manifest)
