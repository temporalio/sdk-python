"""Debug script for Claude Agent SDK integration."""

import asyncio
import logging
from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.claude_agent import (
    ClaudeAgentPlugin,
    ClaudeSessionConfig,
    StatefulClaudeSessionProvider,
)
from temporalio.contrib.claude_agent import workflow as claude_workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

# Enable debug logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')


@workflow.defn
class TestWorkflow:
    @workflow.run
    async def run(self) -> str:
        from claude_agent_sdk import AssistantMessage, TextBlock

        config = ClaudeSessionConfig(
            system_prompt="You are a helpful assistant. Answer concisely.",
            max_turns=1,
        )

        print("Creating session...")
        async with claude_workflow.claude_session("test-session", config):
            print("Session created")

            # Create transport
            transport = claude_workflow.create_claude_transport("test-session")

            # Use the InternalClient to send a query
            from claude_agent_sdk._internal.client import InternalClient

            client = InternalClient()
            responses = []

            # Convert config to options for the client
            options = config.to_claude_options()

            print("Sending query to Claude...")
            async for message in client.process_query(
                prompt="What is 2 + 2? Just give me the number.",
                options=options,
                transport=transport,
            ):
                print(f"Received message: {type(message).__name__}")
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            responses.append(block.text)

            result = " ".join(responses)
            print(f"Claude's response: {result}")
            return result


async def main():
    print("Starting test...")

    # Start Temporal test environment
    async with await WorkflowEnvironment.start_local() as env:
        print("Test environment started")

        # Create session provider
        session_provider = StatefulClaudeSessionProvider("test-session")

        # Create plugin
        plugin = ClaudeAgentPlugin(session_providers=[session_provider])

        # Create client with plugin
        config = env.client.config()
        config["plugins"] = [plugin]
        client = Client(**config)

        print("Starting worker...")

        # Create worker
        async with Worker(
            client,
            task_queue="test-queue",
            workflows=[TestWorkflow],
        ):
            print("Running workflow...")

            # Run workflow
            result = await client.execute_workflow(
                TestWorkflow.run,
                id="test-workflow",
                task_queue="test-queue",
            )

            print(f"Result: {result}")


if __name__ == "__main__":
    asyncio.run(main())