# OpenAI Agents SDK Integration for Temporal

⚠️ **Public Preview** - The interface to this module is subject to change prior to General Availability.
We welcome questions and feedback in the [#python-sdk](https://temporalio.slack.com/archives/CTT84RS0P) Slack channel at [temporalio.slack.com](https://temporalio.slack.com/).


## Introduction

This integration combines [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) with [Temporal's durable execution](https://docs.temporal.io/evaluate/understanding-temporal#durable-execution).
It allows you to build durable agents that never lose their progress and handle long-running, asynchronous, and human-in-the-loop workflows with production-grade reliability.

Temporal and OpenAI Agents SDK are complementary technologies, both of which contribute to simplifying what it takes to build highly capable, high-quality AI systems.
Temporal provides a crash-proof system foundation, taking care of the distributed systems challenges inherent to production agentic systems.
OpenAI Agents SDK offers a lightweight yet powerful framework for defining those agents.

This document is organized as follows:
 - **[Hello World Durable Agent](#hello-world-durable-agent).** Your first durable agent example.
 - **[Background Concepts](#core-concepts).** Background on durable execution and AI agents.
 - **[Full Example](#full-example)** Running the Hello World Durable Agent example.
 - **[Tool Calling](#tool-calling).** Calling agent Tools in Temporal.
 - **[Feature Support](#feature-support).** Compatibility matrix.

The [samples repository](https://github.com/temporalio/samples-python/tree/main/openai_agents) contains examples including basic usage, common agent patterns, and more complete samples.


## Hello World Durable Agent

The code below shows how to wrap an agent for durable execution.

### File 1: Durable Agent (`hello_world.py`)

```python
from temporalio import workflow
from agents import Agent, Runner

@workflow.defn
class HelloWorldAgent:
    @workflow.run
    async def run(self, prompt: str) -> str:
        agent = Agent(
            name="Assistant",
            instructions="You only respond in haikus.",
        )

        result = await Runner.run(agent, input=prompt)
        return result.final_output
```

In this example, Temporal provides the durable execution wrapper: the `HelloWorldAgent.run` method.
The content of that method, is regular OpenAI Agents SDK code.

If you are familiar with Temporal and with Open AI Agents SDK, this code will look very familiar.
The `@workflow.defn` annotation on the `HelloWorldAgent` indicates that this class will contain durable execution logic. The `@workflow.run` annotation defines the entry point.
We use the `Agent` class from OpenAI Agents SDK to define a simple agent, instructing it to always respond with haikus.
We then run that agent, using the `Runner` class from OpenAI Agents SDK, passing through `prompt` as an argument.


We will [complete this example below](#full-example).
Before digging further into the code, we will review some background that will make it easier to understand.

## Background Concepts

We encourage you to review this section thoroughly to gain a solid understanding of AI agents and durable execution with Temporal.
This knowledge will make it easier to design and build durable agents.
If you are already well versed in these topics, feel free to skim this section or skip ahead.

### AI Agents

In the OpenAI Agents SDK, an agent is an AI model configured with instructions, tools, MCP servers, guardrails, handoffs, context, and more.

We describe each of these briefly:

- *AI model*. An LLM such as OpenAI's GPT, Google's Gemini, or one of many others.
- *Instructions*. Also known as a system prompt, the instructions contain the initial input to the model, which configures it for the job it will do.
- *Tools*. Typically, Python functions that the model may choose to invoke. Tools are functions with text-descriptions that explain their functionality to the model.
- *MCP servers*. Best known for providing tools, MCP offers a pluggable standard for interoperability, including file-like resources, prompt templates, and human approvals. MCP servers may be accessed over the network or run in a local process.
- *Guardrails*. Checks on the input or the output of an agent to ensure compliance or safety. Guardrails may be implemented as regular code or as AI agents.
- *Handoffs*. A handoff occurs when an agent delegates a task to another agent. During a handoff the conversation history remains the same, and passes to a new agent with its own model, instructions, tools.
- *Context*. This is an overloaded term. Here, context refers to a framework object that is shared across tools and other code, but is not passed to the model.

Now, let's see how these components work together.
In a common pattern, the model first receives user input and then reasons about which tool to invoke.
The tool's response is passed back to the model, which may call additional tools, repeating this loop until the task is complete.

The diagram below illustrates this flow.

```text
           +-------------------+
           |     User Input    |
           +-------------------+
                     |
                     v
          +---------------------+
          |  Reasoning (Model)  |  <--+
          +---------------------+     |
                     |                |
           (decides which action)     |
                     v                |
          +---------------------+     |
          |       Action        |     |
          | (e.g., use a Tool)  |     |
          +---------------------+     |
                     |                |
                     v                |
          +---------------------+     |
          |     Observation     |     |
          |    (Tool Output)    |     |
          +---------------------+     |
                     |                |
                     +----------------+
          (loop: uses new info to reason
           again, until task is complete)
```

Even in a simple example like this, there are many places where things can go wrong.
Tools call APIs that sometimes fail, while models can encounter rate limits, requiring retries.
The longer the agent runs, the more costly it is to start the job over.
We next describe durable execution, which handles such failures seamlessly.

### Durable Execution

In Temporal's durable execution implementation, a program that crashes or encounters an exception while interacting with a model or API will retry until it can successfully complete.

Temporal relies primarily on a replay mechanism to recover from failures.
As the program makes progress, Temporal saves key inputs and decisions, allowing a re-started program to pick up right where it left off.

The key to making this work is to separate the applications repeatable (deterministic) and non-repeatable (non-deterministic) parts:

1. Deterministic pieces, termed *workflows*, execute the same way when re-run with the same inputs.
2. Non-deterministic pieces, termed *activities*, can run arbitrary code, performing I/O and any other operations.

Workflow code can run for extended periods and, if interrupted, resume exactly where it left off.
Activity code faces no restrictions on I/O or external interactions, but if it fails part-way through it restarts from the beginning.

In the AI-agent example above, model invocations and tool calls run inside activities, while the logic that coordinates them lives in the workflow.
This pattern generalizes to more sophisticated agents.
We refer to that coordinating logic as *agent orchestration*.

As a general rule, agent orchestration code executes within the Temporal workflow, whereas model calls and any I/O-bound tool invocations execute as Temporal activities.

The diagram below shows the overall architecture of an agentic application in Temporal.
The Temporal Server is responsible to tracking program execution and making sure associated state is preserved reliably (i.e., stored to a database, possibly replicated across cloud regions).
Temporal Server manages data in encrypted form, so all data processing occurs on the Worker, which runs the workflow and activities.


```text
            +---------------------+
            |   Temporal Server   |      (Stores workflow state,
            +---------------------+       schedules activities,
                     ^                    persists progress)
                     |
        Save state,  |   Schedule Tasks,
        progress,    |   load state on resume
        timeouts     |
                     |
+------------------------------------------------------+
|                      Worker                          |
|   +----------------------------------------------+   |
|   |              Workflow Code                   |   |
|   |       (Agent Orchestration Loop)             |   |
|   +----------------------------------------------+   |
|          |          |                |               |
|          v          v                v               |
|   +-----------+ +-----------+ +-------------+        |
|   | Activity  | | Activity  | |  Activity   |        |
|   | (Tool 1)  | | (Tool 2)  | | (Model API) |        |
|   +-----------+ +-----------+ +-------------+        |
|         |           |                |               |
+------------------------------------------------------+
          |           |                |
          v           v                v
      [External APIs, services, databases, etc.]
```


See the [Temporal documentation](https://docs.temporal.io/evaluate/understanding-temporal#temporal-application-the-building-blocks) for more information.


## Complete Example

To make the [Hello World durable agent](#hello-world-durable-agent) shown earlier available in Temporal, we need to create a worker program.
To see it run, we also need a client to launch it.
We show these files below.


### File 2: Launch Worker (`run_worker.py`)

```python
# File: run_worker.py

import asyncio
from datetime import timedelta

from temporalio.client import Client
from temporalio.contrib.openai_agents import OpenAIAgentsPlugin, ModelActivityParameters
from temporalio.worker import Worker

from hello_world_workflow import HelloWorldAgent


async def worker_main():
    # Use the plugin to configure Temporal for use with OpenAI Agents SDK
    client = await Client.connect(
        "localhost:7233",
        plugins=[
            OpenAIAgentsPlugin(
                model_params=ModelActivityParameters(
                    start_to_close_timeout=timedelta(seconds=30)
                )
            ),
        ],
    )

    worker = Worker(
        client,
        task_queue="my-task-queue",
        workflows=[HelloWorldAgent],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(worker_main())
```

We use the `OpenAIAgentsPlugin` to configure Temporal for use with OpenAI Agents SDK.
The plugin automatically handles several important setup tasks:
- Ensures proper serialization of Pydantic types
- Propagates context for [OpenAI Agents tracing](https://openai.github.io/openai-agents-python/tracing/).
- Registers an activity for invoking model calls with the Temporal worker.
- Configures OpenAI Agents SDK to run model calls as Temporal activities.


### File 3: Client Execution (`run_hello_world_workflow.py`)

```python
# File: run_hello_world_workflow.py

import asyncio

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.contrib.openai_agents import OpenAIAgentsPlugin

from hello_world_workflow import HelloWorldAgent

async def main():
    # Create client connected to server at the given address
    client = await Client.connect(
        "localhost:7233",
        plugins=[OpenAIAgentsPlugin()],
    )

    # Execute a workflow
    result = await client.execute_workflow(
        HelloWorldAgent.run,
        "Tell me about recursion in programming.",
        id="my-workflow-id",
        task_queue="my-task-queue",
        id_reuse_policy=WorkflowIDReusePolicy.TERMINATE_IF_RUNNING,
    )
    print(f"Result: {result}")

if __name__ == "__main__":
    asyncio.run(main())
```

This file is a standard Temporal launch script.
We also configure the client with the `OpenAIAgentsPlugin` to ensure serialization is compatible with the worker.


To run this example, see the detailed instructions in the [Temporal Python Samples Repository](https://github.com/temporalio/samples-python/tree/main/openai_agents).

## Tool Calling

### Temporal Activities as OpenAI Agents Tools

One of the powerful features of this integration is the ability to convert Temporal activities into agent tools using `activity_as_tool`.
This allows your agent to leverage Temporal's durable execution for tool calls.

In the example below, we apply the `@activity.defn` decorator to the `get_weather` function to create a Temporal activity.
We then pass this through the `activity_as_tool` helper function to create an OpenAI Agents tool that is passed to the `Agent`.

```python
from dataclasses import dataclass
from datetime import timedelta
from temporalio import activity, workflow
from temporalio.contrib import openai_agents
from agents import Agent, Runner

@dataclass
class Weather:
    city: str
    temperature_range: str
    conditions: str

@activity.defn
async def get_weather(city: str) -> Weather:
    """Get the weather for a given city."""
    return Weather(city=city, temperature_range="14-20C", conditions="Sunny with wind.")

@workflow.defn
class WeatherAgent:
    @workflow.run
    async def run(self, question: str) -> str:
        agent = Agent(
            name="Weather Assistant",
            instructions="You are a helpful weather agent.",
            tools=[
                openai_agents.workflow.activity_as_tool(
                    get_weather, 
                    start_to_close_timeout=timedelta(seconds=10)
                )
            ],
        )
        result = await Runner.run(starting_agent=agent, input=question)
        return result.final_output
```

### Calling OpenAI Agents Tools inside Temporal Workflows

For simple computations that don't involve external calls you can call the tool directly from the workflow by using the standard OpenAI Agents SDK `@functiontool` annotation.

```python
from temporalio import workflow
from agents import Agent, Runner
from agents import function_tool

@function_tool
def calculate_circle_area(radius: float) -> float:
    """Calculate the area of a circle given its radius."""
    import math
    return math.pi * radius ** 2

@workflow.defn
class MathAssistantAgent:
    @workflow.run
    async def run(self, message: str) -> str:
        agent = Agent(
            name="Math Assistant",
            instructions="You are a helpful math assistant. Use the available tools to help with calculations.",
            tools=[calculate_circle_area],
        )
        result = await Runner.run(agent, input=message)
        return result.final_output
```

Note that any tools that run in the workflow must respect the workflow execution restrictions, meaning no I/O or non-deterministic operations.
Of course, code running in the workflow can invoke a Temporal activity at any time.

Tools that run in the workflow can also update OpenAI Agents context, which is read-only for tools run as Temporal activities.


## MCP Support

This integration provides support for Model Context Protocol (MCP) servers through two wrapper approaches designed to handle different implications of failures.

While Temporal provides durable execution for your workflows, this durability does not extend to MCP servers, which operate independently of the workflow and must provide their own durability. The integration handles this by offering stateless and stateful wrappers that you can choose based on your MCP server's design.

### Stateless vs Stateful MCP Servers

You need to understand your MCP server's behavior to choose the correct wrapper:

**Stateless MCP servers** treat each operation independently. For example, a weather server with a `get_weather(location)` tool is stateless because each call is self-contained and includes all necessary information. These servers can be safely restarted or reconnected to without changing their behavior.

**Stateful MCP servers** maintain session state between calls. For example, a weather server that requires calling `set_location(location)` followed by `get_weather()` is stateful because it remembers the configured location and uses it for subsequent calls. If the session or the server is restarted, state crucial for operation is lost. Temporal identifies such failures and raises an `ApplicationError` to signal the need for application-level failure handling.

### Usage Example (Stateless MCP)

The code below gives an example of using a stateless MCP server.

#### Worker Configuration

```python
import asyncio
from datetime import timedelta
from agents.mcp import MCPServerStdio
from temporalio.client import Client
from temporalio.contrib.openai_agents import (
    ModelActivityParameters,
    OpenAIAgentsPlugin,
    StatelessMCPServerProvider,
)
from temporalio.worker import Worker


async def main():
    # Create the MCP server provider
    filesystem_server = StatelessMCPServerProvider(
        lambda: MCPServerStdio(
            name="FileSystemServer",
            params={
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/files"],
            },
        )
    )

    # Register the MCP server with the OpenAI Agents plugin
    client = await Client.connect(
        "localhost:7233",
        plugins=[
            OpenAIAgentsPlugin(
                model_params=ModelActivityParameters(
                    start_to_close_timeout=timedelta(seconds=60)
                ),
                mcp_server_providers=[filesystem_server],
            ),
        ],
    )

    worker = Worker(
        client,
        task_queue="my-task-queue",
        workflows=[FileSystemWorkflow],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
```

#### Workflow Implementation

```python
from temporalio import workflow
from temporalio.contrib import openai_agents
from agents import Agent, Runner

@workflow.defn
class FileSystemWorkflow:
    @workflow.run
    async def run(self, query: str) -> str:
        # Reference the MCP server by name (matches name in worker configuration)
        server = openai_agents.workflow.stateless_mcp_server("FileSystemServer")
        
        agent = Agent(
            name="File Assistant",
            instructions="Use the filesystem tools to read files and answer questions.",
            mcp_servers=[server],
        )
        
        result = await Runner.run(agent, input=query)
        return result.final_output
```

The `StatelessMCPServerProvider` takes a factory function that creates new MCP server instances. The server name used in `stateless_mcp_server()` must match the name configured in the MCP server instance. In this example, the name is `FileSystemServer`.

### Stateful MCP Servers

For implementation details and examples, see the [samples repository](https://github.com/temporalio/samples-python/tree/main/openai_agents/mcp).

When using stateful servers, the dedicated worker maintaining the connection may fail due to network issues or server problems. When this happens, Temporal raises an `ApplicationError` and cannot automatically recover because it cannot restore the lost server state.
To recover from such failures, you need to implement your own application-level retry logic.

### Hosted MCP Tool

For network-accessible MCP servers, you can also use `HostedMCPTool` from the OpenAI Agents SDK, which uses an MCP client hosted by OpenAI.

## Feature Support

This integration is presently subject to certain limitations.
Streaming and voice agents are not supported.
Certain tools are not suitable for a distributed computing environment, so these have been disabled as well.

### Model Providers

| Model Provider | Supported |
|:--------------|:---------:|
| OpenAI        |    Yes    |
| LiteLLM       |    Yes    |

### Model Response format

This integration does not presently support streaming.

| Model Response | Supported |
|:--------------|:---------:|
| Get Response  |    Yes    |
| Streaming     |    No     |

### Tools

#### Tool Type

`LocalShellTool` and `ComputerTool` are not suited to a distributed computing setting.

| Tool Type           | Supported |
|:-------------------|:---------:|
| FunctionTool        |    Yes    |
| LocalShellTool      |    No     |
| WebSearchTool       |    Yes    |
| FileSearchTool      |    Yes    |
| HostedMCPTool       |    Yes    |
| ImageGenerationTool |    Yes    |
| CodeInterpreterTool |    Yes    |
| ComputerTool        |    No     |

#### Tool Context

As described in [Tool Calling](#tool-calling), context propagation is read-only when Temporal activities are used as tools.

| Context Propagation                     | Supported |
|:----------------------------------------|:---------:|
| Activity Tool receives copy of context  |    Yes    |
| Activity Tool can update context        |    No     |
| Function Tool received context          |    Yes    |
| Function Tool can update context        |    Yes    |

### MCP

The MCP protocol is stateful, but many MCP servers are stateless.
We let you choose between two MCP wrappers, one designed for stateless MCP servers and one for stateful MCP servers.
These wrappers work with all transport varieties.

Note that when using network-accessible MCP servers, you also can also use the tool `HostedMCPTool`, which is part of the OpenAI Responses API and uses an MCP client hosted by OpenAI.

| MCP Class              | Supported |
|:-----------------------|:---------:|
| MCPServerStdio         |    Yes    |
| MCPServerSse           |    Yes    |
| MCPServerStreamableHttp|    Yes    |

### Guardrails

| Guardrail Type | Supported |
|:---------------|:---------:|
| Code           |    Yes    |
| Agent          |    Yes    |

### Sessions

SQLite storage is not suited to a distributed environment.

| Feature        | Supported |
|:---------------|:---------:|
| SQLiteSession  |    No     |

### Tracing

| Tracing Provider | Supported |
|:-----------------|:---------:|
| OpenAI platform  |    Yes    |

### Voice 

| Mode                    | Supported |
|:------------------------|:---------:|
| Voice agents (pipelines)|    No     |
| Realtime agents         |    No     |

### Utilities

The REPL utility is not suitable for a distributed setting.

| Utility | Supported |
|:--------|:---------:|
| REPL    |    No     |


## Additional Examples

You can find additional examples in the [Temporal Python Samples Repository](https://github.com/temporalio/samples-python/tree/main/openai_agents).

