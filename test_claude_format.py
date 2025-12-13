import asyncio
import json
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

async def test_message_format():
    """Test to see actual message format Claude SDK uses"""
    
    # Create a mock transport to capture what gets sent
    class MockTransport:
        def __init__(self):
            self.messages = []
        
        async def connect(self):
            pass
        
        async def write(self, data):
            print(f"Transport.write called with: {data}")
            self.messages.append(data)
    
    client = ClaudeSDKClient()
    
    # Monkey-patch to intercept transport writes
    mock = MockTransport()
    
    # Test what query() actually sends
    client._transport = mock
    client._query = True  # Fake being connected
    
    await client.query("Test message")
    await client.query("Another message", session_id="custom-session")

asyncio.run(test_message_format())
