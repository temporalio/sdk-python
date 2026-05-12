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
* Wrap the model with `TemporalModel` so the LLM call runs as a durable activity. Pass the real model to `StrandsPlugin` on the worker:
  ```python
  # workflow
  from temporalio.contrib.strands import TemporalModel

  agent = Agent(model=TemporalModel(start_to_close_timeout=timedelta(seconds=60)))

  # worker
  from strands.models.bedrock import BedrockModel
  from temporalio.contrib.strands import StrandsPlugin

  Worker(..., plugins=[StrandsPlugin(model=BedrockModel(model_id="claude-3-5-sonnet"))])
  ```
  To stream chunks to external consumers, pass `streaming_topic="..."` to `TemporalModel` and host a `WorkflowStream` on the workflow.
