# OpenAI Agents SDK Integration for Temporal

⚠️ **Public Preview** - The interface to this module is subject to change prior to General Availability. We welcome your questions and feedback in the [#python-sdk](https://temporalio.slack.com/archives/CTT84RS0P) Slack channel at [temporalio.slack.com](https://temporalio.slack.com/).


## Introduction

This integration combines [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) with [Temporal's durable execution](https://docs.temporal.io/evaluate/understanding-temporal#durable-execution).
It allows you to build AI agents that never lose their progress and handle long-running, asynchronous, and human-in-the-loop workflows with ease.

Temporal provides a crash-proof system foundation, taking care of the distributed systems challenges inherent to production agentic systems.
OpenAI Agents SDK offers a lightweight yet powerful framework for defining those agents.
The combination lets you build reliable agentic systems quickly.

This document is organized as follows:
 - **[Hello World Agent](#hello-world-durable-agent).** Your first durable agent example.
 - **[Background Concepts](#core-concepts).** Background on durable execution and AI agents.
 - **[Complete Example](#complete-example)** Complete example.
 - **Usage Guide.**
 - **Agent Patterns.**

The [samples repository](https://github.com/temporalio/samples-python/tree/main/openai_agents) contains a number of examples spanning various use cases.


## Hello World Durable Agent

The code below shows how straightforward it is to wrap an agent wrapped in durable execution.

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

If you are familiar with Temporal and with Open AI Agents SDK, this code will look very familiar.
We annotate the `HelloWorldAgent` class with `@workflow.defn` to define a workflow, then use the `@workflow.run` annotation to define the entrypoint.

We use the `Agent` class to define a simple agent, instructing it to always responds with haikus.
Within the workflow, we start the agent using the `Runner`, as is typical, passing through `prompt` as an argument.

We will [complete this example below](#complete-example).
However, before digging further into the code, it we will share some more background to set the stage.

## Background Concepts

We encourage you to form a thorough understanding of AI agents and durable execution with Temporal.
Understanding this will make it easer to design and build durable agents.
If you are well versed in these topics, you may skim this section or skip ahead.

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


Now, let's look at how these pieces can fit together.
In one popular pattern, the model receives user input, then performs reasoning to select a tool to call.
The response from the tool is fed back into the model, which may perform additional tool calls, iterating until the task is complete.

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

Even in a simple example like this, there are many places where something can go wrong.
Tools call APIs that are sometimes down and models have rate limits, requiring retries.
The longer the agent runs, the more costly it is to start the job over.
In the next section, we turn to durable execution, which can handle such failures seamlessly.

### Durable Execution

In Temporal's durable execution implementation, a program that crashes or encounters an exception while interacting with a model or API will retry until it can successfully complete.

Temporal relies heavily on a replay mechanism to recover from failures.
As the program makes progress, Temporal saves key inputs and decisions, allowing a re-started program to pick up right where it left off.

The key to making this work is to separate the applications repeatable (deterministic) and non-repeatable (non-deterministic) parts:

1. Deterministic pieces, termed *workflows*, execute the same way if re-run with the same inputs.
2. Non-deterministic pieces, termed *activities*, have no limitations—they may perform I/O and any other operations.

In the AI agent described in the previous section, model and tool calls run in activities, and the control flow linking them together runs in the workflow.

In more complex examples, the control flow may be described as *agent orchestration*.
Agent orchestration runs within the Temporal workflow, while model calls and any tool calls involving I/O run in activities.

The diagram below shows the overall architecture of an agentic application in Temporal.
The Temporal Server is responsible to tracking program execution and making sure associated state is preserved reliably.
Temporal Server manages data in encrypted form.
All data processing occurs on the Worker, which runs the workflow and activities.


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

To make the [Hello World durable agent](#hello-world-durable-agent) available in Temporal, we need to create a worker program.
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
- Ensures proper serialization by of Pydantic types
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

## Using Temporal Activities as OpenAI Agents Tools

One of the powerful features of this integration is the ability to convert Temporal activities into OpenAI Agents tools using `activity_as_tool`.
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

## Calling Tools Directly

For simple computations that don't involve external calls you can call the tool directly from the workflow:

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

Note that any tools designed to run in the workflow must respect the workflow execution restrictions, meaning no I/O or non-deterministic operations.
Of course, you can always invoke an activity from the workflow if needed.


## Agent Handoffs

The OpenAI Agents SDK supports agent handoffs, where one agent transfers control of execution to another agent.
In this example, one Temporal workflow wraps the multi-agent system:

```python
@workflow.defn
class CustomerServiceWorkflow:
    def __init__(self):
        self.current_agent = self.init_agents()

    def init_agents(self):
        faq_agent = Agent(
            name="FAQ Agent",
            instructions="Answer frequently asked questions",
        )
        
        booking_agent = Agent(
            name="Booking Agent", 
            instructions="Help with booking and seat changes",
        )
        
        triage_agent = Agent(
            name="Triage Agent",
            instructions="Route customers to the right agent",
            handoffs=[faq_agent, booking_agent],
        )
        
        return triage_agent

    @workflow.run
    async def run(self, customer_message: str) -> str:
        result = await Runner.run(
            starting_agent=self.current_agent,
            input=customer_message,
            context=self.context,
        )
        return result.final_output
```


## Additional Examples

You can find additional examples in the [Temporal Python Samples Repository](https://github.com/temporalio/samples-python/tree/main/openai_agents).
