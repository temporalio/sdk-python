import temporalio.contrib.openai_agents as compatibility
import temporalio.contrib.openai_agents.testing as compatibility_testing
import temporalio.contrib.openai_agents.workflow as compatibility_workflow
import temporalio.openai_agents as standalone
import temporalio.openai_agents.testing as standalone_testing
import temporalio.openai_agents.workflow as standalone_workflow


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
