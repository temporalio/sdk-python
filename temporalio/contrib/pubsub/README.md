# Temporal Workflow Pub/Sub

> ⚠️ **This package is currently at an experimental release stage.** ⚠️

Workflows sometimes need to push incremental updates to external observers.
Examples include providing customer updates during order processing, creating
interactive experiences with AI agents, or reporting progress from a
long-running data pipeline. Temporal's core primitives (workflows, signals, and
updates) already provide the building blocks, but wiring up batching, offset
tracking, topic filtering, and continue-as-new hand-off is non-trivial.

This module packages that boilerplate into a reusable broker and client. The
workflow acts as a message broker that maintains an append-only log.
Applications can interact directly from the workflow, or from external clients
such as activities, starters, and other workflows. Under the hood, publishing
uses signals (fire-and-forget) while subscribing uses updates (long-poll). A
configurable batching coalesces high-frequency events, improving efficiency.

Payloads are Temporal `Payload`s carrying the encoding metadata needed for
typed decode (`subscribe(result_type=T)`) and heterogeneous-topic dispatch
(`Payload.metadata`). The codec chain (e.g. encryption, compression)
runs once on the signal/update envelope that carries each
batch — **not** per item — so there is no double-encryption, and codec
behavior is symmetric between workflow-side and client-side publishing.

## Quick Start

### Workflow side

Construct a `PubSub` from your `@workflow.init`. The constructor
dynamically registers the pub/sub signal, update, and query handlers on
the current workflow, and raises `RuntimeError` if called twice. If you
want the workflow to support continue-as-new, include a
`PubSubState | None` field on the input and pass it through — it's
`None` on fresh starts and carries state across CAN otherwise:

```python
from dataclasses import dataclass
from temporalio import workflow
from temporalio.contrib.pubsub import PubSub, PubSubState

@dataclass
class MyInput:
    pubsub_state: PubSubState | None = None

@workflow.defn
class MyWorkflow:
    @workflow.init
    def __init__(self, input: MyInput) -> None:
        self.pubsub = PubSub(prior_state=input.pubsub_state)

    @workflow.run
    async def run(self, input: MyInput) -> None:
        self.pubsub.publish("status", StatusEvent(state="started"))
        await do_work()
        self.pubsub.publish("status", StatusEvent(state="done"))
```

Both workflow-side and client-side `publish()` use the sync payload
converter for per-item `Payload` construction. The codec chain runs
once at the envelope level (`__temporal_pubsub_publish` signal,
`__temporal_pubsub_poll` update) — never per item — so encryption,
compression, and any other codec transforms are applied once each way.

### Activity side (publishing)

Use `PubSubClient.from_activity()` with the async context manager for
batched publishing. The Temporal client and target workflow ID are taken
from the activity context:

```python
from datetime import timedelta

from temporalio import activity
from temporalio.contrib.pubsub import PubSubClient

@activity.defn
async def stream_events() -> None:
    client = PubSubClient.from_activity(batch_interval=timedelta(seconds=2))
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

Use `await client.flush()` when you need proof that prior publications have
reached the server before continuing — for example, before returning from an
activity:

```python
async with client:
    for chunk in generate_chunks():
        client.publish("events", chunk)
    # Ensure everything is confirmed before the activity completes.
    await client.flush()
    do_something_that_depends_on_delivery()
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
from dataclasses import dataclass, field
from temporalio import workflow
from temporalio.contrib.pubsub import PubSub, PubSubState

@dataclass
class AppState:
    # Whatever your workflow needs to carry forward.
    ...

@dataclass
class WorkflowInput:
    app_state: AppState = field(default_factory=AppState)
    pubsub_state: PubSubState | None = None

@workflow.defn
class MyWorkflow:
    @workflow.init
    def __init__(self, input: WorkflowInput) -> None:
        self.app_state = input.app_state
        self.pubsub = PubSub(prior_state=input.pubsub_state)

    @workflow.run
    async def run(self, input: WorkflowInput) -> None:
        # ... do work, updating self.app_state ...

        if workflow.info().is_continue_as_new_suggested():
            await self.pubsub.continue_as_new(lambda pubsub_state: [WorkflowInput(
                app_state=self.app_state,
                pubsub_state=pubsub_state,
            )])
```

`PubSub.continue_as_new(build_args)` drains waiting subscribers,
waits for in-flight handlers to finish, then calls
`workflow.continue_as_new` with `build_args(post_drain_state)`. The
lambda receives the post-drain `PubSubState` so the snapshot is
guaranteed to happen *after* drain. Subscribers created via
`PubSubClient.create()` or `PubSubClient.from_activity()` automatically
follow continue-as-new chains.

Workflows that need to pass other CAN parameters (`task_queue`,
`retry_policy`, `run_timeout`, etc.) fall back to the explicit recipe:

```python
self.pubsub.drain()
await workflow.wait_condition(workflow.all_handlers_finished)
workflow.continue_as_new(args=[WorkflowInput(
    app_state=self.app_state,
    pubsub_state=self.pubsub.get_state(),
)], task_queue="other-tq")
```

## Gotcha: sync handlers racing `__temporal_pubsub_publish`

If you add a **custom synchronous** `@workflow.update` or
`@workflow.signal` handler that reads `PubSub` state, and an
external client calls `handle.signal("__temporal_pubsub_publish", ...)`
immediately followed by that handler, the handler may observe
pre-publish state when both land in the same workflow activation.
Root cause: `PubSub` installs `__temporal_pubsub_publish` *dynamically* from
`@workflow.init`, so in the first activation the signal is buffered
until after your class-level handler has already been scheduled.

Two framings for when you need to care:

- If your producer and your update caller are **independent
  services** (the common shape for `PubSub`), the handler already
  has to be robust to "update arrived before publish" for reasons
  unrelated to this race — network timing, missing publishes, bad
  offsets. Whatever policy you have for those covers this race too.
- If your code does **sequential same-client** ordering — await
  `handle.signal(...)`, then await `handle.execute_update(...)` on
  the same handle, and expect the signal's effects to be visible —
  use the recipe below.

### Recipe

Make the handler `async` and yield once before touching `PubSub`
state:

```python
import asyncio
from temporalio import workflow

@workflow.defn
class MyWorkflow:
    @workflow.init
    def __init__(self) -> None:
        self.pubsub = PubSub()

    @workflow.update
    async def truncate_at(self, offset: int) -> None:
        await asyncio.sleep(0)               # let pending publishes apply
        self.pubsub.truncate(offset)         # now sees post-signal state
```

`asyncio.sleep(0)` is a pure asyncio-level yield — one event-loop
tick, no Temporal timer, no history events, no server round trip.
Do **not** substitute `workflow.sleep(0)`; that schedules a Temporal
timer and adds history events on every call.

Already-safe patterns, no recipe needed:

- The module's own `__temporal_pubsub_poll` update (it is already `async` and
  `await`s `workflow.wait_condition` internally).
- Any `async` handler that `await`s something before reading
  `PubSub` state.
- Handlers whose semantics are naturally "wait for the state I'm
  asking about" — use `await workflow.wait_condition(lambda: ...)`
  with a meaningful predicate instead of `asyncio.sleep(0)`.
- Workflow-internal publishes (`self.pubsub.publish(...)` from
  `run()` or from an activity); these do not race.

## API Reference

### PubSub

| Method | Description |
|---|---|
| `PubSub(prior_state=None)` | Constructor. Call once from `@workflow.init`; registers handlers on the current workflow. Raises `RuntimeError` if a `PubSub` is already registered. Pass `prior_state` if the input declares one (`None` on fresh starts). |
| `publish(topic, value)` | Append to the log from workflow code. `value` is converted via the sync workflow payload converter (no codec). |
| `get_state(*, publisher_ttl=timedelta(seconds=900))` | Snapshot for continue-as-new. Drops publisher dedup entries older than `publisher_ttl` (a `timedelta`, default 15 minutes). |
| `drain()` | Unblock polls and reject new ones. |
| `continue_as_new(build_args, *, publisher_ttl=timedelta(seconds=900))` | Async. Drain, wait for handlers, then `workflow.continue_as_new` with `build_args(post_drain_state)`. Use the explicit recipe to override other CAN parameters. |
| `truncate(up_to_offset)` | Discard log entries below the given offset. Workflow-side only — no external API; wire up your own signal or update if external control is needed. |

Handlers registered by the constructor:

| Kind | Name | Description |
|---|---|---|
| Signal | `__temporal_pubsub_publish` | Receive external publications. |
| Update | `__temporal_pubsub_poll` | Long-poll subscription. |
| Query | `__temporal_pubsub_offset` | Current global offset. |

### PubSubClient

| Method | Description |
|---|---|
| `PubSubClient.create(client, workflow_id, *, batch_interval, max_batch_size, max_retry_duration)` | Factory with an explicit Temporal client and workflow id. Follows CAN. |
| `PubSubClient.from_activity(*, batch_interval, max_batch_size, max_retry_duration)` | Factory that takes client and workflow id from the current activity context. Follows CAN. |
| `PubSubClient(handle, *, batch_interval, max_batch_size, max_retry_duration)` | From handle (no CAN follow). |
| `publish(topic, value, force_flush=False)` | Buffer a message. `value` may be any converter-compatible object or a pre-built `Payload`. Per-item conversion uses the sync payload converter; the codec chain runs once on the signal envelope. |
| `subscribe(topics, from_offset, *, result_type=None, poll_cooldown=timedelta(milliseconds=100))` | Async iterator. With `result_type=T`, `item.data` is decoded to `T`; otherwise it is a raw `Payload`. Follows CAN chains when created via `create` or `from_activity`. |
| `get_offset()` | Query current global offset. |

Use as `async with` for batched publishing with automatic flush.

## Cross-Language Protocol

Any Temporal client can interact with a pub/sub workflow using these
fixed handler names:

1. **Publish:** Signal `__temporal_pubsub_publish` with `PublishInput`
2. **Subscribe:** Update `__temporal_pubsub_poll` with `PollInput` -> `PollResult`
3. **Offset:** Query `__temporal_pubsub_offset` -> `int`

The Python API exposes Temporal `Payload`s and decodes via the client's
data converter. On the wire, each `PublishEntry.data` / `_WireItem.data`
is a base64-encoded `Payload.SerializeToString()` so the transport
remains JSON-serializable while preserving `Payload.metadata` (used by
codecs and by the decode path). Cross-language clients can publish and
subscribe by following the same base64-of-serialized-`Payload` shape.
The signal/update envelopes (`PublishInput`, `PollResult`, `PubSubState`)
require the default (JSON) data converter; custom converters on the
envelope layer will break cross-language interop.
