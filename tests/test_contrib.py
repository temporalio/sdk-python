from types import ModuleType

import pytest

import temporalio.contrib


def test_openai_agents_migration_error() -> None:
    with pytest.raises(
        ImportError,
        match=r"uv add temporalio-openai-agents",
    ):
        exec("from temporalio.contrib import openai_agents", {})


def test_openai_agents_standalone_module(monkeypatch: pytest.MonkeyPatch) -> None:
    standalone_module = ModuleType("temporalio.contrib.openai_agents")
    monkeypatch.setattr(
        temporalio.contrib,
        "_import_module",
        lambda name: standalone_module,
    )
    assert getattr(temporalio.contrib, "openai_agents") is standalone_module


def test_unknown_attribute_error() -> None:
    with pytest.raises(AttributeError, match="does_not_exist"):
        getattr(temporalio.contrib, "does_not_exist")
