# Temporal Workflow Pub/Sub

Reusable pub/sub for Temporal workflows. The workflow acts as a message broker
that maintains an append-only log. External clients (activities, starters, other
workflows, other services) publish and subscribe through the workflow handle
using Temporal primitives.

The Python API uses `bytes` for payloads. Base64 encoding is used internally
on the wire for cross-language compatibility.

## Quick Start

### Workflow side

Add `PubSubMixin` to your workflow and call `init_pubsub()` during initialization:

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

Use `PubSubClient.create()` with the async context manager for batched publishing.
When called from within an activity, the client and workflow ID are inferred automatically:

```python
from temporalio import activity
from temporalio.contrib.pubsub import PubSubClient

@activity.defn
async def stream_events() -> None:
    client = PubSubClient.create(batch_interval=2.0)
    async with client:
        for chunk in generate_chunks():
            client.publish("events", chunk)
            activity.heartbeat()
        # Buffer is flushed automatically on context manager exit
```

Use `priority=True` to trigger an immediate flush for latency-sensitive events:

```python
client.publish("events", data, priority=True)
```

### Subscribing

Use `PubSubClient.create()` and the `subscribe()` async iterator:

```python
from temporalio.contrib.pubsub import PubSubClient

client = PubSubClient.create(temporal_client, workflow_id)
async for item in client.subscribe(["events"], from_offset=0):
    print(item.topic, item.data)
    if is_done(item):
        break
```

## Topics

Topics allow subscribers to receive a subset of the messages in the pub/sub system.
Subscribers can request a list of specific topics, or provide an empty list to receive
messages from all topics. Publishing to a topic implicitly creates it.

## Continue-as-new

Carry both your application state and pub/sub state across continue-as-new
boundaries:

```python
from dataclasses import dataclass
from temporalio import workflow
from temporalio.contrib.pubsub import PubSubMixin, PubSubState

@dataclass
class WorkflowInput:
    # Your application state
    items_processed: int = 0
    # Pub/sub state
    pubsub_state: PubSubState | None = None

@workflow.defn
class MyWorkflow(PubSubMixin):
    @workflow.init
    def __init__(self, input: WorkflowInput) -> None:
        self.items_processed = input.items_processed
        self.init_pubsub(prior_state=input.pubsub_state)

    @workflow.run
    async def run(self, input: WorkflowInput) -> None:
        # ... do work, updating self.items_processed ...

        if workflow.info().is_continue_as_new_suggested():
            self.drain_pubsub()
            await workflow.wait_condition(workflow.all_handlers_finished)
            workflow.continue_as_new(args=[WorkflowInput(
                items_processed=self.items_processed,
                pubsub_state=self.get_pubsub_state(),
            )])
```

`drain_pubsub()` unblocks waiting subscribers and rejects new polls so
`all_handlers_finished` can stabilize. Subscribers created via
`PubSubClient.create()` automatically follow continue-as-new chains.

## API Reference

### PubSubMixin

| Method | Description |
|---|---|
| `init_pubsub(prior_state=None)` | Initialize state. Call in `__init__` for fresh workflows, or in `run()` when accepting CAN state. |
| `publish(topic, data)` | Append to the log from workflow code. |
| `get_pubsub_state(*, publisher_ttl=900.0)` | Snapshot for continue-as-new. Drops publisher dedup entries older than `publisher_ttl` seconds. |
| `drain_pubsub()` | Unblock polls and reject new ones. |
| `truncate_pubsub(up_to_offset)` | Discard log entries below the given offset. |

Handlers added automatically:

| Kind | Name | Description |
|---|---|---|
| Signal | `__pubsub_publish` | Receive external publications. |
| Update | `__pubsub_poll` | Long-poll subscription. |
| Query | `__pubsub_offset` | Current global offset. |

### PubSubClient

| Method | Description |
|---|---|
| `PubSubClient.create(client, workflow_id, *, batch_interval, max_batch_size, max_retry_duration)` | Factory. Auto-detects activity context if args omitted. |
| `PubSubClient(handle, *, batch_interval, max_batch_size, max_retry_duration)` | From handle (no CAN follow). |
| `publish(topic, data, priority=False)` | Buffer a message. |
| `subscribe(topics, from_offset, poll_cooldown=0.1)` | Async iterator. Always follows CAN chains when created via `create`. |
| `get_offset()` | Query current global offset. |

Use as `async with` for batched publishing with automatic flush.

## Cross-Language Protocol

Any Temporal client can interact with a pub/sub workflow using these
fixed handler names:

1. **Publish:** Signal `__pubsub_publish` with `PublishInput`
2. **Subscribe:** Update `__pubsub_poll` with `PollInput` -> `PollResult`
3. **Offset:** Query `__pubsub_offset` -> `int`
