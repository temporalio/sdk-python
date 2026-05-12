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
