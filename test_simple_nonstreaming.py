"""Test non-streaming mode to verify basic flow."""

import asyncio
import logging
from claude_agent_sdk import query, ClaudeAgentOptions

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

async def test_nonstreaming():
    """Test non-streaming mode."""

    options = ClaudeAgentOptions(
        system_prompt="You are a helpful assistant. Keep responses brief."
    )

    logger.info("Sending first query")

    # First query
    result1 = ""
    async for message in query(prompt="What is 2 + 2?", options=options):
        msg_type = message.get("type") if hasattr(message, "get") else type(message).__name__
        logger.info(f"Q1 - Received: {msg_type}")
        logger.debug(f"Q1 - Full message: {message}")
        if msg_type == "AssistantMessage":
            # The message is already parsed by the SDK
            logger.info(f"Assistant message attributes: {dir(message)}")
            logger.info(f"Assistant message dict: {message.__dict__ if hasattr(message, '__dict__') else 'no dict'}")

            # Try different ways to get content
            if hasattr(message, "content"):
                for block in message.content:
                    logger.info(f"Content block: {block}")
                    if hasattr(block, "text"):
                        result1 += block.text

    logger.info(f"First query result: {result1[:100]}")

    # Second query (new context)
    logger.info("Sending second query")
    result2 = ""
    async for message in query(prompt="What is 3 + 3?", options=options):
        msg_type = message.get("type") if hasattr(message, "get") else type(message).__name__
        logger.info(f"Q2 - Received: {msg_type}")
        if msg_type == "AssistantMessage":
            # Handle message object
            if hasattr(message, "message") and hasattr(message.message, "content"):
                for block in message.message.content:
                    if hasattr(block, "text"):
                        result2 += block.text

    logger.info(f"Second query result: {result2[:100]}")

asyncio.run(test_nonstreaming())