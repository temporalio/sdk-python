"""Test to debug the message format issue."""

import asyncio
import json
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

async def test_format():
    """Test message format with ClaudeSDKClient."""

    options = ClaudeAgentOptions(
        system_prompt="You are a helpful assistant. Keep responses brief."
    )

    client = ClaudeSDKClient(options)

    # Connect with no initial prompt (streaming mode)
    await client.connect()

    print("Connected, sending first query")

    # Send first query
    await client.query("What is 2 + 2?")

    # Read responses
    result1 = ""
    message_count = 0
    async for message in client.receive_messages():
        print(f"Message {message_count}: type={type(message).__name__}")
        message_count += 1

        # Handle AssistantMessage
        if type(message).__name__ == "AssistantMessage":
            print(f"  Assistant content: {message.content}")
            for block in message.content:
                if hasattr(block, "text"):
                    result1 += block.text

        # Stop after getting assistant response
        if type(message).__name__ == "AssistantMessage":
            break

        # Safety limit
        if message_count > 10:
            break

    print(f"First query result: {result1}")

    # Send second query
    print("Sending second query")
    await client.query("Now multiply that by 3")

    # Read responses
    result2 = ""
    message_count = 0
    async for message in client.receive_messages():
        print(f"Message {message_count}: type={type(message).__name__}")
        message_count += 1

        # Handle AssistantMessage
        if type(message).__name__ == "AssistantMessage":
            print(f"  Assistant content: {message.content}")
            for block in message.content:
                if hasattr(block, "text"):
                    result2 += block.text

        # Stop after getting assistant response
        if type(message).__name__ == "AssistantMessage":
            break

        # Safety limit
        if message_count > 10:
            break

    print(f"Second query result: {result2}")

    # Disconnect
    await client.disconnect()

asyncio.run(test_format())