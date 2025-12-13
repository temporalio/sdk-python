"""Test the basic infrastructure for Claude Agent SDK integration."""

import asyncio
import logging

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.claude_agent import (
    ClaudeAgentPlugin,
    ClaudeSessionConfig,
    StatefulClaudeSessionProvider,
)
from temporalio.contrib.claude_agent._claude_session_workflow import ClaudeSessionWorkflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

# Enable debug logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')


@workflow.defn
class InfrastructureTestWorkflow:
    @workflow.run
    async def run(self) -> str:
        """Test that basic infrastructure works."""
        config = ClaudeSessionConfig(
            system_prompt="Test",
            max_turns=1,
        )

        workflow_id = f"test-session@{workflow.info().run_id}-workflow"
        print(f"Starting workflow: {workflow_id}")

        # Start the workflow as a child workflow
        handle = await workflow.start_child_workflow(
            ClaudeSessionWorkflow.run,
            config,
            id=workflow_id,
            task_queue=f"test-session@{workflow.info().run_id}",
        )

        print("Sending test message via signal...")
        # Send a test message via signal
        test_message = '{"type": "test", "content": "hello"}\n'
        await handle.signal(ClaudeSessionWorkflow.send_message, test_message)

        print("Reading messages via update...")
        # Try to read messages via update
        messages = await handle.execute_update(ClaudeSessionWorkflow.receive_messages)

        print(f"Received messages: {messages}")

        # Signal shutdown
        print("Sending shutdown signal...")
        await handle.signal(ClaudeSessionWorkflow.shutdown)

        return f"Infrastructure test passed. Received {len(messages)} messages."


async def main():
    print("Starting infrastructure test...")

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
            workflows=[InfrastructureTestWorkflow],
        ):
            print("Running workflow...")

            # Run workflow
            result = await client.execute_workflow(
                InfrastructureTestWorkflow.run,
                id="test-infrastructure-workflow",
                task_queue="test-queue",
            )

            print(f"Result: {result}")


if __name__ == "__main__":
    asyncio.run(main())