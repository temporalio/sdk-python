"""Test streaming mode with proper message format."""

import asyncio
import json
import logging
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._internal.query import Query
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

async def test_streaming():
    """Test streaming mode with proper message format."""

    # Create message queue
    message_queue = asyncio.Queue()

    async def message_stream():
        """Yield messages for Claude."""
        while True:
            msg = await message_queue.get()
            if msg is None:
                break
            logger.info(f"Yielding message: {msg}")
            yield msg

    # Create transport and query
    options = ClaudeAgentOptions(
        system_prompt="You are a helpful assistant. Keep responses brief."
    )

    transport = SubprocessCLITransport(
        prompt=message_stream(),
        options=options,
    )

    await transport.connect()

    query = Query(
        transport=transport,
        is_streaming_mode=True,
        can_use_tool=None,
        hooks=None,
        sdk_mcp_servers={},
    )

    await query.start()
    await query.initialize()

    logger.info("Query initialized, sending first message")

    # Send first message
    message1 = {
        "type": "user",
        "message": {"role": "user", "content": "What is 2 + 2?"},
        "parent_tool_use_id": None,
        "session_id": "default"
    }
    await message_queue.put(message1)

    # Read some responses
    response_count = 0
    async for response in query.receive_messages():
        logger.info(f"Response {response_count}: {response.get('type')}")
        response_count += 1
        if response_count > 5:  # Limit responses for testing
            break

    logger.info("Sending second message")

    # Send second message
    message2 = {
        "type": "user",
        "message": {"role": "user", "content": "Now multiply that by 3"},
        "parent_tool_use_id": None,
        "session_id": "default"
    }
    await message_queue.put(message2)

    # Read more responses
    response_count = 0
    async for response in query.receive_messages():
        logger.info(f"Response {response_count}: {response.get('type')}")
        response_count += 1
        if response_count > 5:  # Limit responses for testing
            break

    # Cleanup
    await message_queue.put(None)
    await query.close()
    await transport.close()

    logger.info("Test complete")

asyncio.run(test_streaming())