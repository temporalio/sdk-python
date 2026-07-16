# Google Gemini SDK Integration for Temporal

> ⚠️ **Experimental.** This integration may change in future versions. Use with
> caution in production.

## Overview

This plugin lets you use the [Google Gemini SDK](https://googleapis.github.io/python-genai/)
(`google-genai`) inside Temporal workflows with durable execution. Every Gemini
API call becomes a **Temporal activity**, so model calls, tool calls, file
operations, interactions, and managed agents are retried, recorded in history,
and survive worker restarts.

Key properties:

- **Credentials never enter the workflow.** The real `genai.Client` lives only
  on the worker, inside activities; no API keys or tokens appear in event
  history.
- **The SDK's automatic function calling (AFC) loop runs in the workflow**, so
  tool wrappers (`activity_as_tool`) work naturally — no manual agent loop.
- **Temporal owns retries.** Configure them via the activity `retry_policy`; the
  SDK's own retry loop is rejected to avoid double-retry (see
  [Retries & errors](#retries--errors)).

## Install

```bash
uv add temporalio google-genai
# For client-side MCP support, also:
uv add mcp
```

## Hello World

```python
import os
from datetime import timedelta

from google import genai
from google.genai import types

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.google_genai import (
    GoogleGenAIPlugin,
    TemporalAsyncClient,
    activity_as_tool,
)
from temporalio.worker import Worker
from temporalio.workflow import ActivityConfig


# ---- a tool, as a normal Temporal activity (runs on the worker) ----
@activity.defn
async def get_weather(city: str) -> str:
    return f"It's sunny in {city}."


# ---- the workflow (runs in the Temporal sandbox) ----
@workflow.defn
class WeatherAgent:
    @workflow.run
    async def run(self, prompt: str) -> str:
        client = TemporalAsyncClient()
        response = await client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[
                    activity_as_tool(
                        get_weather,
                        activity_config=ActivityConfig(
                            start_to_close_timeout=timedelta(seconds=30),
                        ),
                    ),
                ],
            ),
        )
        return response.text or ""


# ---- worker setup (outside the sandbox: real client + credentials) ----
async def main() -> None:
    gemini = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    plugin = GoogleGenAIPlugin(gemini)

    client = await Client.connect("localhost:7233", plugins=[plugin])
    async with Worker(
        client,
        task_queue="gemini",
        workflows=[WeatherAgent],
        activities=[get_weather],
    ):
        result = await client.execute_workflow(
            WeatherAgent.run,
            "What's the weather in Tokyo?",
            id="weather-1",
            task_queue="gemini",
        )
        print(result)
```

Construct `TemporalAsyncClient` **inside** the workflow; construct the real
`genai.Client` and `GoogleGenAIPlugin` **on the worker**.

## What this plugin gives you

| Surface | Workflow API | Runs as |
| --- | --- | --- |
| Model calls | `client.models.generate_content` / `generate_content_stream` | activity (AFC loop in workflow) |
| Tools | `activity_as_tool(fn, ...)` | one activity per tool call |
| Files | `client.files.upload` / `download` | activity |
| File search | `client.file_search_stores.upload_to_file_search_store` | activity |
| Interactions | `client.interactions.create` / `get` / `cancel` / `delete` | whole-operation activity |
| Managed agents | `client.agents.create` / `get` / `list` / `delete` | whole-operation activity |
| MCP (client-side) | `TemporalMcpClientSession(name)` in `tools=[...]` | `list_tools` / `call_tool` activities |

Streamed responses are batched: the activity drains the stream and the workflow
iterates the collected chunks/events. `client.webhooks` is not supported in
workflows and raises.

## Tool calling

`activity_as_tool` wraps any `@activity.defn` function as a Gemini tool. When the
model calls it, the AFC loop (running in the workflow) dispatches it as a
durable activity:

```python
activity_as_tool(
    get_weather,
    activity_config=ActivityConfig(start_to_close_timeout=timedelta(seconds=30)),
)
```

A timeout is required — `activity_config` must set `start_to_close_timeout` or
`schedule_to_close_timeout` (Temporal needs one; there is no default for tools).

## MCP support

Client-side MCP (Gemini Developer API) is wired through the plugin: register the
server on the worker and reference it by name in the workflow.

```python
from contextlib import asynccontextmanager
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from temporalio.contrib.google_genai import TemporalMcpClientSession


# ---- worker: a factory yielding a connected, initialized session ----
@asynccontextmanager
async def weather_mcp():
    params = StdioServerParameters(command=sys.executable, args=["weather_server.py"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


plugin = GoogleGenAIPlugin(
    genai.Client(api_key=os.environ["GOOGLE_API_KEY"]),
    mcp_servers={"weather": weather_mcp},
    mcp_connection_idle_timeout=timedelta(minutes=5),
)


# ---- workflow: reference the server by name in the tools list ----
@workflow.defn
class McpAgent:
    @workflow.run
    async def run(self, prompt: str) -> str:
        client = TemporalAsyncClient()
        session = TemporalMcpClientSession(
            "weather",
            activity_config=ActivityConfig(start_to_close_timeout=timedelta(seconds=30)),
        )
        response = await client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(tools=[session]),
        )
        return response.text or ""
```

The MCP connection lives on the worker (pooled, idle-evicted); the workflow only
carries the server name. Tool discovery and calls run as `{name}-list-tools` /
`{name}-call-tool` activities, so the full tool parameter schema reaches the
model. Set `cache_tools=True` to list a server's tools once per workflow instead
of per turn.

## Streaming

`generate_content_stream` works as usual — the workflow iterates chunks (batched
from the activity). To let an **external** consumer (a chat UI) observe chunks in
real time while the workflow runs durably, set `streaming_topic` on the client
and host a [`WorkflowStream`](../workflow_streams/) in the workflow. Each
streamed `GenerateContentResponse` is published to that topic as it arrives:

```python
from temporalio.contrib.workflow_streams import WorkflowStream


@workflow.defn
class StreamingAgent:
    @workflow.init
    def __init__(self, prompt: str) -> None:
        self.stream = WorkflowStream()  # required when streaming_topic is set

    @workflow.run
    async def run(self, prompt: str) -> str:
        client = TemporalAsyncClient(streaming_topic="gemini")
        text = []
        async for chunk in await client.models.generate_content_stream(
            model="gemini-2.5-flash", contents=prompt,
        ):
            text.append(chunk.text or "")
        return "".join(text)
```

Consume the stream from outside the workflow:

```python
from temporalio.contrib.workflow_streams import WorkflowStreamClient


async def consume(client, workflow_id):
    stream = WorkflowStreamClient.create(client, workflow_id)
    async for item in stream.subscribe(
        ["gemini"], result_type=types.GenerateContentResponse,
    ):
        print(item.data.text, end="", flush=True)
```

The workflow's own iteration is unchanged (it still receives batched chunks for
the SDK to parse); the topic is purely for external real-time observation. If
`streaming_topic` is set but the workflow hosts no `WorkflowStream`, the call
raises `GoogleGenAIError`. Tune flush cadence with
`TemporalAsyncClient(streaming_topic=..., streaming_batch_interval=...)`
(default 100ms).

## Retries & errors

Temporal owns retries. Configure them with the activity `retry_policy` via
`activity_config`. The plugin **rejects** the SDK's own retry config so retries
don't compound:

- Constructing the plugin with a `genai.Client` that has
  `http_options.retry_options` raises `ValueError`.
- Setting `http_options.retry_options` on a per-request call raises
  `GoogleGenAIError`.

API-call activities classify failures: transient statuses (408, 429, 5xx) stay
retryable (the activity's `retry_policy` applies); other statuses (e.g. 4xx) are
non-retryable so the workflow fails fast.

## Vertex AI

Pass `vertexai=True` to both the worker-side `genai.Client` and the
workflow-side `TemporalAsyncClient`. On the workflow side you must also set
`project` and `location` **explicitly**:

```python
# worker
genai.Client(vertexai=True, project="my-project", location="us-central1")

# workflow
TemporalAsyncClient(vertexai=True, project="my-project", location="us-central1")
```

Normally the SDK auto-discovers `project`/`location` from the environment
(credentials, ADC, metadata server). That discovery
would be non-deterministic and break replay. Setting them by hand
keeps it deterministic.

## Composing with other plugins

`GoogleGenAIPlugin` is a `temporalio.plugin.SimplePlugin`; pass it in the
`plugins=[...]` list alongside others (e.g. OpenTelemetry). It contributes a
Pydantic data converter, the Gemini activities, a sandbox-passthrough config for
`google.genai` (and `mcp`), and registers `GoogleGenAIError` as a workflow
failure type. When composing data converters, construct the plugins so their
converters are compatible.
