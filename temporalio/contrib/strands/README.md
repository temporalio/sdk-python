# AWS Strands Agents

# Unsupported features
* File-based tool lookup

# Migration Guide
* Mark non-deterministic tools with `as_activity()` and include in `StrandsAgent(activity_tools=[...])`
* Use `agent.invoke_async(message)` instead of `agent(message)` which spawns a thread
