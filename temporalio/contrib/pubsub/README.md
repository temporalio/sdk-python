# Temporal Workflow Pub/Sub

Workflows sometimes need to push incremental updates to external observers.
Examples include providing customer updates during order processing, creating
interactive experiences with AI agents, or reporting progress from a
long-running data pipeline. Temporal's core primitives (workflows, signals, and
updates) already provide the building blocks, but wiring up batching, offset
tracking, topic filtering, and continue-as-new hand-off is non-trivial.

This module packages that boilerplate into a reusable mixin and client. The
workflow acts as a message broker that maintains an append-only log.
Applications can interact directly from the workflow, or from external clients
such as activities, starters, and other workflows. Under the hood, publishing
uses signals (fire-and-forget) while subscribing uses updates (long-poll). A
configurable batching coalesces high-frequency events, improving efficiency.

Payloads are Temporal `Payload`s carrying the encoding metadata needed for
typed decode (`subscribe(result_type=T)`) and heterogeneous-topic dispatch
(`Payload.metadata`). The codec chain (encryption, PII-redaction,
compression) runs once on the signal/update envelope that carries each
batch — **not** per item — so there is no double-encryption, and codec
behavior is symmetric between workflow-side and client-side publishing.

## Quick Start

### Workflow side

Add `PubSubMixin` to your workflow and call `init_pubsub()` from
`@workflow.init`. If you want the workflow to support continue-as-new,
include a `PubSubState | None` field on the input and pass it through —
it's `None` on fresh starts and carries state across CAN otherwise:

```python
from dataclasses import dataclass
from temporalio import workflow
from temporalio.contrib.pubsub import PubSubMixin, PubSubState

@dataclass
class MyInput:
    pubsub_state: PubSubState | None = None

@workflow.defn
class MyWorkflow(PubSubMixin):
    @workflow.init
    def __init__(self, input: MyInput) -> None:
        self.init_pubsub(prior_state=input.pubsub_state)

    @workflow.run
    async def run(self, input: MyInput) -> None:
        self.publish("status", StatusEvent(state="started"))
        await do_work()
        self.publish("status", StatusEvent(state="done"))
```

Both workflow-side and client-side `publish()` use the sync payload
converter for per-item `Payload` construction. The codec chain runs
once at the envelope level (`__pubsub_publish` signal,
`__pubsub_poll` update) — never per item — so encryption,
PII-redaction, and compression are applied once each way.

### Activity side (publishing)

Use `PubSubClient.from_activity()` with the async context manager for
batched publishing. The Temporal client and target workflow ID are taken
from the activity context:

```python
from temporalio import activity
from temporalio.contrib.pubsub import PubSubClient

@activity.defn
async def stream_events() -> None:
    client = PubSubClient.from_activity(batch_interval=2.0)
    async with client:
        for chunk in generate_chunks():
            client.publish("events", chunk)
            activity.heartbeat()
        # Buffer is flushed automatically on context manager exit
```

Use `force_flush=True` to trigger an immediate flush for latency-sensitive events:

```python
client.publish("events", data, force_flush=True)
```

### Subscribing

Use `PubSubClient.create()` and the `subscribe()` async iterator:

```python
from temporalio.contrib.pubsub import PubSubClient

client = PubSubClient.create(temporal_client, workflow_id)
async for item in client.subscribe(["events"], result_type=MyEvent):
    print(item.topic, item.data)
    if is_done(item):
        break
```

`item.data` is a `temporalio.api.common.v1.Payload` when no
`result_type` is given; passing `result_type=T` decodes each item to
`T` via the client's data converter (including the codec chain).

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
`PubSubClient.create()` or `PubSubClient.from_activity()` automatically
follow continue-as-new chains.

## API Reference

### PubSubMixin

| Method | Description |
|---|---|
| `init_pubsub(prior_state=None)` | Initialize state. Call from `@workflow.init`, passing `prior_state` if the input declares one (`None` on fresh starts). |
| `publish(topic, value)` | Append to the log from workflow code. `value` is converted via the sync workflow payload converter (no codec). |
| `get_pubsub_state(*, publisher_ttl=900.0)` | Snapshot for continue-as-new. Drops publisher dedup entries older than `publisher_ttl` seconds. |
| `drain_pubsub()` | Unblock polls and reject new ones. |
| `truncate_pubsub(up_to_offset)` | Discard log entries below the given offset. Workflow-side only — no external API; wire up your own signal or update if external control is needed. |

Handlers added automatically:

| Kind | Name | Description |
|---|---|---|
| Signal | `__pubsub_publish` | Receive external publications. |
| Update | `__pubsub_poll` | Long-poll subscription. |
| Query | `__pubsub_offset` | Current global offset. |

### PubSubClient

| Method | Description |
|---|---|
| `PubSubClient.create(client, workflow_id, *, batch_interval, max_batch_size, max_retry_duration)` | Factory with an explicit Temporal client and workflow id. Follows CAN. |
| `PubSubClient.from_activity(*, batch_interval, max_batch_size, max_retry_duration)` | Factory that takes client and workflow id from the current activity context. Follows CAN. |
| `PubSubClient(handle, *, batch_interval, max_batch_size, max_retry_duration)` | From handle (no CAN follow). |
| `publish(topic, value, force_flush=False)` | Buffer a message. `value` may be any converter-compatible object or a pre-built `Payload`. Per-item conversion uses the sync payload converter; the codec chain runs once on the signal envelope. |
| `subscribe(topics, from_offset, *, result_type=None, poll_cooldown=0.1)` | Async iterator. With `result_type=T`, `item.data` is decoded to `T`; otherwise it is a raw `Payload`. Follows CAN chains when created via `create` or `from_activity`. |
| `get_offset()` | Query current global offset. |

Use as `async with` for batched publishing with automatic flush.

## Cross-Language Protocol

Any Temporal client can interact with a pub/sub workflow using these
fixed handler names:

1. **Publish:** Signal `__pubsub_publish` with `PublishInput`
2. **Subscribe:** Update `__pubsub_poll` with `PollInput` -> `PollResult`
3. **Offset:** Query `__pubsub_offset` -> `int`

The Python API exposes Temporal `Payload`s and decodes via the client's
data converter. On the wire, each `PublishEntry.data` / `_WireItem.data`
is a base64-encoded `Payload.SerializeToString()` so the transport
remains JSON-serializable while preserving `Payload.metadata` (used by
codecs and by the decode path). Cross-language clients can publish and
subscribe by following the same base64-of-serialized-`Payload` shape.
The signal/update envelopes (`PublishInput`, `PollResult`, `PubSubState`)
require the default (JSON) data converter; custom converters on the
envelope layer will break cross-language interop.
