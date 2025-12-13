"""Test multi-turn conversations with Claude Agent SDK integration."""

import asyncio
import logging

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.claude_agent import (
    ClaudeAgentPlugin,
    ClaudeMessageReceiver,
    ClaudeSessionConfig,
    SimplifiedClaudeClient,
    StatefulClaudeSessionProvider,
)
from temporalio.contrib.claude_agent import workflow as claude_workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

# Enable debug logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')


@workflow.defn
class MultiTurnClaudeWorkflow(ClaudeMessageReceiver):
    @workflow.run
    async def run(self) -> str:
        """Workflow that tests multi-turn conversation."""
        # Initialize the message receiver
        self.init_claude_receiver()
        config = ClaudeSessionConfig(
            system_prompt="You are a helpful assistant. Keep responses brief.",
            max_turns=5,
        )

        print("Creating session...")
        async with claude_workflow.claude_session("test-session", config):
            print("Session created successfully!")

            # Create simplified client
            client = SimplifiedClaudeClient(self)

            print("Client connected")

            # First turn
            print("Sending first query: What is 2 + 2?")
            result1 = ""
            async for message in client.send_query("What is 2 + 2?"):
                print(f"Turn 1 - Received: {message.get('type')}")
                if message.get("type") == "assistant":
                    for block in message.get("content", []):
                        if block.get("type") == "text":
                            result1 += block.get("text", "")

            # Second turn - follow-up question
            result2 = ""
            async for message in client.send_query("Now multiply that by 3"):
                print(f"Turn 2 - Received: {message.get('type')}")
                if message.get("type") == "assistant":
                    for block in message.get("content", []):
                        if block.get("type") == "text":
                            result2 += block.get("text", "")

            # Close the session properly
            await client.close()

            return f"Turn 1: {result1[:100]}\nTurn 2: {result2[:100]}"


async def main():
    print("Starting multi-turn Claude test...")

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
            workflows=[MultiTurnClaudeWorkflow],
        ):
            print("Running workflow...")

            # Run workflow
            try:
                result = await client.execute_workflow(
                    MultiTurnClaudeWorkflow.run,
                    id="multiturn-claude-workflow",
                    task_queue="test-queue",
                )

                print(f"Result:\n{result}")
            except Exception as e:
                print(f"Error: {e}")
                import traceback
                traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())