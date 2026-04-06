# Temporal Workflow Pub/Sub

Reusable pub/sub for Temporal workflows. The workflow acts as a message broker
with an append-only log. External clients (activities, starters, other services)
publish and subscribe through the workflow handle using Temporal primitives.

Payloads are opaque byte strings for cross-language compatibility.

## Quick Start

### Workflow side

Add `PubSubMixin` to your workflow and call `init_pubsub()`:

```python
from temporalio import workflow
from temporalio.contrib.pubsub import PubSubMixin

@workflow.defn
class MyWorkflow(PubSubMixin):
    @workflow.init
    def __init__(self, input: MyInput) -> None:
        self.init_pubsub()

    @workflow.run
    async def run(self, input: MyInput) -> None:
        self.publish("status", b"started")
        await do_work()
        self.publish("status", b"done")
```

### Activity side (publishing)

Use `activity_pubsub_client()` with the async context manager for batched
publishing:

```python
from temporalio import activity
from temporalio.contrib.pubsub import activity_pubsub_client

@activity.defn
async def stream_events() -> None:
    client = activity_pubsub_client(batch_interval=2.0)
    async with client:
        for chunk in generate_chunks():
            client.publish("events", chunk)
            activity.heartbeat()
        # Buffer is flushed automatically on context manager exit
```

Use `priority=True` to flush immediately for latency-sensitive events:

```python
client.publish("events", data, priority=True)
```

### Subscribing

Use `PubSubClient.for_workflow()` and the `subscribe()` async iterator:

```python
from temporalio.contrib.pubsub import PubSubClient

client = PubSubClient.for_workflow(temporal_client, workflow_id)
async for item in client.subscribe(["events"], from_offset=0):
    print(item.offset, item.topic, item.data)
    if is_done(item):
        break
```

## Topics

Topics are plain strings with exact matching. No hierarchy or wildcards.

- Publish to one topic at a time
- Subscribe to a list of topics (empty list = all topics)
- Publishing to a topic implicitly creates it

## Continue-as-new

Carry pub/sub state across continue-as-new boundaries:

```python
from dataclasses import dataclass
from temporalio import workflow
from temporalio.contrib.pubsub import PubSubMixin, PubSubState

@dataclass
class WorkflowInput:
    pubsub_state: PubSubState | None = None

@workflow.defn
class MyWorkflow(PubSubMixin):
    @workflow.init
    def __init__(self, input: WorkflowInput) -> None:
        self.init_pubsub(prior_state=input.pubsub_state)

    @workflow.run
    async def run(self, input: WorkflowInput) -> None:
        # ... do work ...

        if workflow.info().is_continue_as_new_suggested():
            self.drain_pubsub()
            await workflow.wait_condition(workflow.all_handlers_finished)
            workflow.continue_as_new(args=[WorkflowInput(
                pubsub_state=self.get_pubsub_state(),
            )])
```

`drain_pubsub()` unblocks waiting subscribers and rejects new polls so
`all_handlers_finished` can stabilize. Subscribers created via
`PubSubClient.for_workflow()` automatically re-target the new run.

**Important:** When using Pydantic models for workflow input, type the field
as `PubSubState | None`, not `Any`. Pydantic deserializes `Any` fields as
plain dicts, which breaks `init_pubsub()`.

## API Reference

### PubSubMixin

| Method | Description |
|---|---|
| `init_pubsub(prior_state=None)` | Initialize state. Call in `__init__`. |
| `publish(topic, data)` | Append to the log from workflow code. |
| `get_pubsub_state()` | Snapshot for continue-as-new. |
| `drain_pubsub()` | Unblock polls and reject new ones. |

Handlers added automatically:

| Handler | Kind | Name |
|---|---|---|
| Signal | `__pubsub_publish` | Receive external publications |
| Update | `__pubsub_poll` | Long-poll subscription |
| Query | `__pubsub_offset` | Current log length |

### PubSubClient

| Method | Description |
|---|---|
| `PubSubClient.for_workflow(client, wf_id)` | Factory (preferred) |
| `PubSubClient(handle)` | From handle (no CAN follow) |
| `publish(topic, data, priority=False)` | Buffer a message |
| `flush()` | Send buffered messages |
| `subscribe(topics, from_offset)` | Async iterator |
| `get_offset()` | Query current offset |

Use as `async with` for batched publishing with automatic flush.

### activity_pubsub_client()

Convenience for creating a `PubSubClient` inside an activity, pre-configured
with the parent workflow's handle and client.

## Cross-Language Protocol

Any Temporal client can interact with a pub/sub workflow using these
fixed handler names:

1. **Publish:** Signal `__pubsub_publish` with `PublishInput`
2. **Subscribe:** Update `__pubsub_poll` with `PollInput` → `PollResult`
3. **Offset:** Query `__pubsub_offset` → `int`
