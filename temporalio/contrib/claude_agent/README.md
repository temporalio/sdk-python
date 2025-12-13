# Claude Agent SDK Integration for Temporal

This module provides seamless integration between the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) and [Temporal](https://temporal.io) workflows, enabling durable, stateful AI-powered workflows.

## Features

- **Stateful Sessions**: Maintain conversation context across workflow executions
- **Multi-turn Conversations**: Support for extended dialogues with Claude
- **Workflow-Safe**: All Claude SDK operations run in activities outside the workflow sandbox
- **Tool Support**: Configure allowed/disallowed tools for Claude to use
- **Automatic Serialization**: Built-in Pydantic data converters for type-safe message passing

## Installation

```bash
pip install temporalio[claude-agent]
```

## Quick Start

### 1. Create a Workflow with Claude Integration

```python
from temporalio import workflow
from temporalio.contrib.claude_agent import (
    ClaudeMessageReceiver,
    ClaudeSessionConfig,
    SimplifiedClaudeClient,
    workflow as claude_workflow,
)

@workflow.defn
class MyClaudeWorkflow(ClaudeMessageReceiver):
    @workflow.run
    async def run(self, prompt: str) -> str:
        # Initialize the message receiver
        self.init_claude_receiver()

        # Configure Claude session
        config = ClaudeSessionConfig(
            system_prompt="You are a helpful assistant.",
            max_turns=10,
        )

        # Create Claude session
        async with claude_workflow.claude_session("my-session", config):
            # Create client for communication
            client = SimplifiedClaudeClient(self)

            # Send query and process responses
            result = ""
            async for message in client.send_query(prompt):
                if message.get("type") == "assistant":
                    for block in message.get("content", []):
                        if block.get("type") == "text":
                            result += block.get("text", "")

            return result
```

### 2. Set Up Worker with Claude Plugin

```python
from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.contrib.claude_agent import (
    ClaudeAgentPlugin,
    StatefulClaudeSessionProvider,
)

async def main():
    # Create session provider
    session_provider = StatefulClaudeSessionProvider("my-session")

    # Create plugin
    plugin = ClaudeAgentPlugin(session_providers=[session_provider])

    # Connect client with plugin
    client = await Client.connect(
        "localhost:7233",
        plugins=[plugin]
    )

    # Create worker
    async with Worker(
        client,
        task_queue="claude-queue",
        workflows=[MyClaudeWorkflow],
    ):
        # Worker is running
        await asyncio.sleep(3600)
```

## Multi-turn Conversations

The integration supports multi-turn conversations within a single session:

```python
@workflow.defn
class ConversationWorkflow(ClaudeMessageReceiver):
    @workflow.run
    async def run(self) -> str:
        self.init_claude_receiver()
        config = ClaudeSessionConfig()

        async with claude_workflow.claude_session("chat", config):
            client = SimplifiedClaudeClient(self)

            # First turn
            async for msg in client.send_query("What is 2 + 2?"):
                # Process response
                pass

            # Second turn - Claude remembers context
            async for msg in client.send_query("Multiply that by 3"):
                # Process response
                pass

            # Close session when done
            await client.close()
```

## Configuration Options

### ClaudeSessionConfig

The `ClaudeSessionConfig` class supports all Claude Agent SDK options:

```python
config = ClaudeSessionConfig(
    # Basic options
    system_prompt="Custom instructions",
    max_turns=20,
    model="claude-3-sonnet-20240229",

    # Tool configuration
    allowed_tools=["read_file", "write_file"],
    disallowed_tools=["run_command"],

    # Permissions
    permission_mode="acceptEdits",  # or 'plan', 'bypassPermissions'

    # Environment
    cwd="/path/to/working/dir",
    env={"CUSTOM_VAR": "value"},

    # Advanced options
    max_thinking_tokens=1000,
    enable_file_checkpointing=True,
)
```

## Tool Usage

Claude can use tools when configured. Currently, only predefined tools are supported:

```python
config = ClaudeSessionConfig(
    allowed_tools=[
        "read_file",
        "write_file",
        "list_files",
        "run_command",
    ],
    disallowed_tools=["delete_file"],  # Explicitly disallow
)
```

Note: Custom tool callbacks cannot cross the workflow-activity boundary. Consider implementing custom tools as separate activities if needed.

## Architecture

The integration uses a three-layer architecture:

1. **Workflow Layer**: Your workflow logic using `ClaudeMessageReceiver` mixin
2. **Activity Layer**: Manages Claude subprocess and message routing
3. **Claude SDK Layer**: The actual Claude Agent SDK running in a subprocess

This design ensures:
- Workflow determinism (all I/O happens in activities)
- State persistence (conversations survive worker restarts)
- Proper isolation (Claude SDK runs outside the workflow sandbox)

## Best Practices

1. **Session Naming**: Use unique, descriptive session names
2. **Error Handling**: Always handle potential errors in message processing
3. **Resource Cleanup**: Call `client.close()` when done with a session
4. **Timeouts**: Configure appropriate activity timeouts for long conversations
5. **Heartbeating**: The activity automatically sends heartbeats during long operations

## Limitations

- **Tool Callbacks**: Custom tool implementations with callbacks are not yet supported
- **MCP Servers**: Model Context Protocol servers are not yet supported
- **Streaming**: Only non-streaming mode is currently supported
- **File Persistence**: File system changes in Claude's environment are not persisted

## Examples

See the `examples/` directory for complete examples:
- `simple_query.py`: Basic single query example
- `multi_turn.py`: Multi-turn conversation example
- `with_tools.py`: Using Claude with tools
- `error_handling.py`: Proper error handling patterns

## Troubleshooting

### Timeout Errors
Increase activity timeout in the session configuration:
```python
from datetime import timedelta

activity_config = ActivityConfig(
    start_to_close_timeout=timedelta(minutes=30),
)

async with claude_workflow.claude_session("session", config, activity_config):
    # Long-running operations
```

### Serialization Errors
Ensure all data passed between workflow and activities is serializable:
- Use `ClaudeSessionConfig` instead of `ClaudeAgentOptions`
- Avoid passing callbacks or file objects
- Use Pydantic models for custom data structures

### Session Not Found
Ensure the session provider name matches the session name used in the workflow:
```python
# Provider
provider = StatefulClaudeSessionProvider("my-session")

# Workflow
async with claude_workflow.claude_session("my-session", config):
    # Must use same name
```

## Contributing

Contributions are welcome! Please see the main Temporal SDK contributing guidelines.

## License

This integration is part of the Temporal Python SDK and follows the same license.