"""Debug test for Claude multi-turn conversations."""

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
logger = logging.getLogger(__name__)


@workflow.defn
class DebugClaudeWorkflow(ClaudeMessageReceiver):
    @workflow.run
    async def run(self) -> str:
        """Workflow that tests multi-turn conversation."""
        workflow.logger.info("Starting workflow")

        # Initialize the message receiver
        self.init_claude_receiver()
        config = ClaudeSessionConfig(
            system_prompt="You are a helpful assistant. Keep responses very brief.",
            max_turns=5,
        )

        workflow.logger.info("Creating session...")
        async with claude_workflow.claude_session("test-session", config):
            workflow.logger.info("Session created successfully!")

            # Create simplified client
            client = SimplifiedClaudeClient(self)
            workflow.logger.info("Client connected")

            # First query
            workflow.logger.info("Sending first query")
            result1 = ""
            async for message in client.send_query("What is 2 + 2?"):
                workflow.logger.info(f"Q1: Received {message.get('type')}")
                if message.get("type") == "assistant":
                    # In streaming mode, assistant message has nested structure
                    inner_message = message.get("message", {})
                    content = inner_message.get("content", [])
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            result1 += block.get("text", "")

            workflow.logger.info(f"First query complete: {result1[:50]}")

            # Second query - multi-turn
            workflow.logger.info("Sending second query")
            result2 = ""
            async for message in client.send_query("Now multiply that by 3"):
                workflow.logger.info(f"Q2: Received {message.get('type')}")
                if message.get("type") == "assistant":
                    # In streaming mode, assistant message has nested structure
                    inner_message = message.get("message", {})
                    content = inner_message.get("content", [])
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            result2 += block.get("text", "")

            workflow.logger.info(f"Second query complete: {result2[:50]}")

            # Close client
            await client.close()

            return f"Q1: {result1[:50]}\nQ2: {result2[:50]}"


async def main():
    logger.info("Starting debug Claude test...")

    # Start Temporal test environment
    async with await WorkflowEnvironment.start_local() as env:
        logger.info("Test environment started")

        # Create session provider
        session_provider = StatefulClaudeSessionProvider("test-session")

        # Create plugin
        plugin = ClaudeAgentPlugin(session_providers=[session_provider])

        # Create client with plugin
        config = env.client.config()
        config["plugins"] = [plugin]
        client = Client(**config)

        logger.info("Starting worker...")

        # Create worker
        async with Worker(
            client,
            task_queue="test-queue",
            workflows=[DebugClaudeWorkflow],
        ):
            logger.info("Running workflow...")

            # Run workflow with timeout
            try:
                result = await asyncio.wait_for(
                    client.execute_workflow(
                        DebugClaudeWorkflow.run,
                        id="debug-claude-workflow",
                        task_queue="test-queue",
                    ),
                    timeout=30.0
                )

                logger.info(f"SUCCESS: {result}")
            except asyncio.TimeoutError:
                logger.error("TIMEOUT: Workflow execution timed out after 30 seconds")
            except Exception as e:
                logger.error(f"ERROR: {e}")
                import traceback
                traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())