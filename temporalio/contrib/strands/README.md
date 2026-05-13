# AWS Strands Agents

# Unsupported features
* File-based tool lookup

# Migration Guide
* Use `agent.invoke_async(message)` instead of `agent(message)` which spawns a thread
* Decorate non-deterministic tools with `@activity.defn` and register them via `Worker(activities=[...])`. Wrap them in the agent with `activity_as_tool()`.
* For tools imported from `strands_tools`, write a thin async wrapper that calls the tool, e.g.:
  ```python
  from strands_tools import current_time

  @activity.defn(name="current_time")
  async def current_time_activity() -> str:
      return current_time.current_time()
  ```
* Construct a `TemporalModel` once with a `model_factory` lambda that returns the real model. Pass it both to the workflow's `Agent(model=...)` and to `StrandsPlugin(model=...)`; the plugin calls the factory at worker startup, and the workflow's `stream()` calls dispatch as Temporal activities.
  ```python
  from strands.models.bedrock import BedrockModel
  from temporalio.contrib.strands import StrandsPlugin, TemporalModel

  MODEL = TemporalModel(
      model_factory=lambda: BedrockModel(model_id="claude-3-5-sonnet"),
      start_to_close_timeout=timedelta(seconds=60),
  )

  # workflow
  @workflow.defn
  class MyWorkflow:
      def __init__(self) -> None:
          self.agent = Agent(model=MODEL)

  # worker
  Worker(..., plugins=[StrandsPlugin(model=MODEL)])
  ```
  To stream chunks to external consumers, pass `streaming_topic="..."` to `TemporalModel` and host a `WorkflowStream` on the workflow.
* For MCP servers, construct `TemporalMCPClient` once at module level and reference the same instance from both the plugin (registers the per-server `{server}-call-tool` activity and connects at worker startup to discover tools) and the workflow's `Agent(tools=[...])`:
  ```python
  from mcp import StdioServerParameters, stdio_client
  from temporalio.contrib.strands import StrandsPlugin, TemporalMCPClient, TemporalModel

  ECHO = TemporalMCPClient(
      server="echo",
      transport_factory=lambda: stdio_client(
          StdioServerParameters(command="...", args=[...]),
      ),
      start_to_close_timeout=timedelta(seconds=30),
  )

  # workflow
  @workflow.defn
  class MyWorkflow:
      def __init__(self) -> None:
          self.agent = Agent(model=MODEL, tools=[ECHO])

  # worker
  Worker(..., plugins=[StrandsPlugin(model=MODEL, mcp_clients=[ECHO])])
  ```
  The plugin connects to the MCP server once at worker startup to enumerate tools. The schema is frozen for the worker's lifetime; restart workers to pick up MCP-server changes. If the MCP server is unavailable at startup, the worker fails to start.
