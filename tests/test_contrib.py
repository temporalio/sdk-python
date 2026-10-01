import importlib
import warnings

import temporalio.openai_agents as standalone  # pyright: ignore[reportMissingImports]
import temporalio.openai_agents.testing as standalone_testing  # pyright: ignore[reportMissingImports]
import temporalio.openai_agents.workflow as standalone_workflow  # pyright: ignore[reportMissingImports]

import temporalio.contrib.openai_agents as compatibility
import temporalio.contrib.openai_agents.testing as compatibility_testing
import temporalio.contrib.openai_agents.workflow as compatibility_workflow


def test_openai_agents_compatibility_imports_without_warnings() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        importlib.reload(compatibility)
        importlib.reload(compatibility_testing)
        importlib.reload(compatibility_workflow)


def test_openai_agents_compatibility_imports() -> None:
    assert compatibility.OpenAIAgentsPlugin is standalone.OpenAIAgentsPlugin
    assert compatibility.OpenAIPayloadConverter is standalone.OpenAIPayloadConverter
    assert compatibility_testing.AgentEnvironment is standalone_testing.AgentEnvironment
    assert (
        compatibility_workflow.activity_as_tool is standalone_workflow.activity_as_tool
    )
    assert (
        compatibility_workflow.temporal_sandbox_client
        is standalone_workflow.temporal_sandbox_client
    )
