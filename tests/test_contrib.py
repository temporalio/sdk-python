import pytest

import temporalio.contrib


def test_openai_agents_migration_error() -> None:
    with pytest.raises(
        ImportError,
        match=r"uv add temporalio-openai-agents",
    ):
        exec("from temporalio.contrib import openai_agents", {})


def test_unknown_attribute_error() -> None:
    with pytest.raises(AttributeError, match="does_not_exist"):
        getattr(temporalio.contrib, "does_not_exist")
