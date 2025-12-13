"""Tests for Claude Agent SDK integration with Temporal."""

import os
import uuid
from datetime import timedelta

import pytest

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.claude_agent import (
    ClaudeAgentPlugin,
    ClaudeMessageReceiver,
    ClaudeSessionConfig,
    StatefulClaudeSessionProvider,
)
from temporalio.contrib.claude_agent import workflow as claude_workflow
from tests.helpers import new_worker


@workflow.defn
class BasicQueryWorkflow(ClaudeMessageReceiver):
    """Basic workflow that queries Claude with a simple question."""

    @workflow.run
    async def run(self, prompt: str) -> str:
        """Run a basic query to Claude.

        Args:
            prompt: The question to ask Claude

        Returns:
            Claude's response text
        """
        # Initialize the message receiver
        self.init_claude_receiver()

        from temporalio.contrib.claude_agent import SimplifiedClaudeClient

        config = ClaudeSessionConfig(
            system_prompt="You are a helpful assistant. Answer concisely.",
            max_turns=1,
        )

        # Create session and client
        async with claude_workflow.claude_session("test-session", config):
            client = SimplifiedClaudeClient(self)

            # Send query and collect response
            result = ""
            async for message in client.send_query(prompt):
                if message.get("type") == "assistant":
                    # Extract text from response
                    content = message.get("message", {}).get("content", [])
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            result += block.get("text", "")

            await client.close()
            return result


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="No Anthropic API key available",
)
async def test_basic_query(client: Client):
    """Test basic query workflow with Claude."""
    # Create session provider
    session_provider = StatefulClaudeSessionProvider("test-session")

    # Create plugin
    plugin = ClaudeAgentPlugin(session_providers=[session_provider])

    # Apply plugin to client
    config = client.config()
    config["plugins"] = [plugin]
    client = Client(**config)

    async with new_worker(
        client, BasicQueryWorkflow, activities=[]
    ) as worker:
        result = await client.execute_workflow(
            BasicQueryWorkflow.run,
            "What is 2 + 2? Just give me the number.",
            id=f"basic-query-workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=60),
        )

        # Claude should respond with something containing "4"
        assert "4" in result


@workflow.defn
class StreamingConversationWorkflow(ClaudeMessageReceiver):
    """Workflow that has a multi-turn conversation with Claude."""

    @workflow.run
    async def run(self, questions: list[str]) -> list[str]:
        """Run a streaming conversation with multiple questions.

        Args:
            questions: List of questions to ask Claude

        Returns:
            List of Claude's responses
        """
        # Initialize the message receiver
        self.init_claude_receiver()

        from temporalio.contrib.claude_agent import SimplifiedClaudeClient

        config = ClaudeSessionConfig(
            system_prompt="You are a helpful assistant. Answer each question concisely.",
            max_turns=len(questions) * 2,  # Allow for back-and-forth
        )

        responses = []

        async with claude_workflow.claude_session("streaming-session", config):
            client = SimplifiedClaudeClient(self)

            # Process each question
            for question in questions:
                result = ""
                async for message in client.send_query(question):
                    if message.get("type") == "assistant":
                        # Extract text from response
                        content = message.get("message", {}).get("content", [])
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                result += block.get("text", "")

                responses.append(result)

            await client.close()

        return responses


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="No Anthropic API key available",
)
async def test_streaming_conversation(client: Client):
    """Test streaming conversation workflow with multiple questions."""
    # Create session provider
    session_provider = StatefulClaudeSessionProvider("streaming-session")

    # Create plugin
    plugin = ClaudeAgentPlugin(session_providers=[session_provider])

    # Apply plugin to client
    config = client.config()
    config["plugins"] = [plugin]
    client = Client(**config)

    async with new_worker(
        client, StreamingConversationWorkflow, activities=[]
    ) as worker:
        questions = [
            "What is 5 + 5? Just give me the number.",
            "What is 10 * 2? Just give me the number.",
        ]

        results = await client.execute_workflow(
            StreamingConversationWorkflow.run,
            questions,
            id=f"streaming-workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=120),
        )

        assert len(results) == 2
        # Claude should respond with the correct answers
        assert "10" in results[0]
        assert "20" in results[1]


@workflow.defn
class WithOptionsWorkflow(ClaudeMessageReceiver):
    """Workflow that uses custom Claude options."""

    @workflow.run
    async def run(self, prompt: str) -> str:
        """Run with custom options.

        Args:
            prompt: The prompt to send

        Returns:
            Claude's response
        """
        # Initialize the message receiver
        self.init_claude_receiver()

        from temporalio.contrib.claude_agent import SimplifiedClaudeClient

        config = ClaudeSessionConfig(
            system_prompt="You are a Python expert. Explain things very simply in one sentence.",
            max_turns=1,
            allowed_tools=["Read"],  # Limit to read-only operations
        )

        async with claude_workflow.claude_session("options-session", config):
            client = SimplifiedClaudeClient(self)

            # Send query and collect response
            result = ""
            async for message in client.send_query(prompt):
                if message.get("type") == "assistant":
                    # Extract text from response
                    content = message.get("message", {}).get("content", [])
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            result += block.get("text", "")

            await client.close()
            return result


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="No Anthropic API key available",
)
async def test_with_options(client: Client):
    """Test workflow with custom Claude options."""
    # Create session provider
    session_provider = StatefulClaudeSessionProvider("options-session")

    # Create plugin
    plugin = ClaudeAgentPlugin(session_providers=[session_provider])

    # Apply plugin to client
    config = client.config()
    config["plugins"] = [plugin]
    client = Client(**config)

    async with new_worker(
        client, WithOptionsWorkflow, activities=[]
    ) as worker:
        result = await client.execute_workflow(
            WithOptionsWorkflow.run,
            "What is Python?",
            id=f"options-workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=60),
        )

        # Should get a response about Python
        assert len(result) > 0
        assert "python" in result.lower() or "programming" in result.lower()


async def test_plugin_duplicate_sessions():
    """Test that plugin rejects duplicate session names."""
    provider1 = StatefulClaudeSessionProvider("duplicate")
    provider2 = StatefulClaudeSessionProvider("duplicate")

    with pytest.raises(ValueError, match="same name"):
        ClaudeAgentPlugin(session_providers=[provider1, provider2])


async def test_plugin_data_converter(client: Client):
    """Test that plugin configures data converter correctly."""
    session_provider = StatefulClaudeSessionProvider("test")
    plugin = ClaudeAgentPlugin(session_providers=[session_provider])

    # Plugin should configure Pydantic payload converter
    from temporalio.contrib.claude_agent import ClaudeAgentPayloadConverter

    # Apply plugin and check data converter
    config = client.config()
    config["plugins"] = [plugin]
    client = Client(**config)
    assert isinstance(
        client.data_converter.payload_converter, ClaudeAgentPayloadConverter
    )


@workflow.defn
class MultiProviderWorkflow(ClaudeMessageReceiver):
    """Workflow that tests multiple session providers can coexist."""

    @workflow.run
    async def run(self, session_name: str, prompt: str) -> str:
        """Run a query using the specified session.

        Args:
            session_name: Name of the session to use
            prompt: The question to ask Claude

        Returns:
            Claude's response text
        """
        # Initialize the message receiver
        self.init_claude_receiver()

        from temporalio.contrib.claude_agent import SimplifiedClaudeClient

        config = ClaudeSessionConfig(
            system_prompt="You are a helpful assistant. Answer concisely.",
            max_turns=1,
        )

        # Create session with the specified name
        async with claude_workflow.claude_session(session_name, config):
            client = SimplifiedClaudeClient(self)

            # Send query and collect response
            result = ""
            async for message in client.send_query(prompt):
                if message.get("type") == "assistant":
                    # Extract text from response
                    content = message.get("message", {}).get("content", [])
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            result += block.get("text", "")

            await client.close()
            return result


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="No Anthropic API key available",
)
async def test_multiple_providers(client: Client):
    """Test that multiple session providers can coexist in the same worker."""
    # Create two providers with different names
    provider1 = StatefulClaudeSessionProvider("session-alpha")
    provider2 = StatefulClaudeSessionProvider("session-beta")

    # Create plugin with both providers - this should NOT raise ValueError
    plugin = ClaudeAgentPlugin(session_providers=[provider1, provider2])

    # Apply plugin to client
    config = client.config()
    config["plugins"] = [plugin]
    client = Client(**config)

    async with new_worker(
        client, MultiProviderWorkflow, activities=[]
    ) as worker:
        # Test using the first session
        result = await client.execute_workflow(
            MultiProviderWorkflow.run,
            args=["session-alpha", "What is 3 + 3? Just give me the number."],
            id=f"multi-provider-workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=60),
        )

        # Claude should respond with something containing "6"
        assert "6" in result
