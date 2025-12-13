"""Test using ClaudeSDKClient for multi-turn."""

import asyncio
import logging
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

async def test_session():
    """Test multi-turn with ClaudeSDKClient."""

    options = ClaudeAgentOptions(
        system_prompt="You are a helpful assistant. Keep responses brief."
    )

    # Create client in streaming mode
    client = ClaudeSDKClient(options)

    logger.info("Connecting client")
    await client.connect()

    logger.info("Client connected, sending first query")

    # First query
    await client.query("What is 2 + 2?")

    result1 = ""
    message_count = 0
    async for message in client.receive_messages():
        logger.info(f"Q1 Message {message_count}: {type(message).__name__}")
        message_count += 1

        if type(message).__name__ == "AssistantMessage":
            for block in message.content:
                if hasattr(block, "text"):
                    result1 += block.text
            break  # Stop after assistant message

        if message_count > 10:
            break

    logger.info(f"First result: {result1}")

    # Second query - should maintain context
    logger.info("Sending second query")
    await client.query("Now multiply that by 3")

    result2 = ""
    message_count = 0
    async for message in client.receive_messages():
        logger.info(f"Q2 Message {message_count}: {type(message).__name__}")
        message_count += 1

        if type(message).__name__ == "AssistantMessage":
            for block in message.content:
                if hasattr(block, "text"):
                    result2 += block.text
            break

        if message_count > 10:
            break

    logger.info(f"Second result: {result2}")

    await client.disconnect()
    logger.info("Done")

asyncio.run(test_session())