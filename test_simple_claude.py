"""Simple test for Claude Agent SDK integration."""

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
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')


@workflow.defn
class SimpleClaudeWorkflow(ClaudeMessageReceiver):
    @workflow.run
    async def run(self) -> str:
        """Simple workflow that tests the infrastructure."""
        # Initialize the message receiver
        self.init_claude_receiver()
        config = ClaudeSessionConfig(
            system_prompt="You are a helpful assistant. Just say 'Hello!'",
            max_turns=1,
        )

        print("Creating session...")
        async with claude_workflow.claude_session("test-session", config):
            print("Session created successfully!")

            # Create simplified client
            client = SimplifiedClaudeClient(self)

            print("Client connected")

            # Try to send a simple query
            result_text = ""
            async for message in client.send_query("Say hello"):
                print(f"Received message type: {message.get('type')}")
                # Handle different message types
                if message.get("type") == "text":
                    result_text += message.get("content", "")
                elif message.get("type") == "content" and isinstance(message.get("content"), list):
                    # Handle assistant messages
                    for block in message.get("content", []):
                        if block.get("type") == "text":
                            result_text += block.get("text", "")

            return result_text if result_text else "Infrastructure test complete"


async def main():
    print("Starting simple Claude test...")

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
            workflows=[SimpleClaudeWorkflow],
        ):
            print("Running workflow...")

            # Run workflow
            try:
                result = await client.execute_workflow(
                    SimpleClaudeWorkflow.run,
                    id="simple-claude-workflow",
                    task_queue="test-queue",
                )

                print(f"Result: {result}")
            except Exception as e:
                print(f"Error: {e}")
                import traceback
                traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())