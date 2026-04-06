# Temporal Workflow Pub/Sub — Design Document

## Overview

A reusable pub/sub module for Temporal workflows. The workflow acts as the message
broker — it holds an append-only log of `(offset, topic, data)` entries. External
clients (activities, starters, other services) publish and subscribe through the
workflow handle using Temporal primitives (signals, updates, queries).

The module ships as `temporalio.contrib.pubsub` in the Python SDK and is designed
to be cross-language compatible. Payloads are opaque byte strings — the workflow
does not interpret them.

## API Surface

### Workflow side — `PubSubMixin`

A mixin class that adds signal, update, and query handlers to any workflow.

```python
from temporalio.contrib.pubsub import PubSubMixin

@workflow.defn
class MyWorkflow(PubSubMixin):
    @workflow.run
    async def run(self, input: MyInput) -> MyOutput:
        self.init_pubsub()
        # The workflow is now a pub/sub broker.
        # It can also publish directly:
        self.publish("status", b"started")
        await do_work()
        self.publish("status", b"done")
```

`PubSubMixin` provides:

| Method / Handler | Kind | Description |
|---|---|---|
| `init_pubsub()` | instance method | Initialize internal state. Must be called before use. |
| `publish(topic, data, priority=False)` | instance method | Append to the log from workflow code. |
| `__pubsub_publish` | `@workflow.signal` | Receives publications from external clients. |
| `__pubsub_poll` | `@workflow.update` | Long-poll subscription: blocks until new items or completion. |
| `__pubsub_offset` | `@workflow.query` | Returns the current log length (next offset). |

Double-underscore prefix on handler names avoids collisions with application signals/updates.

### Client side — `PubSubClient`

Used by activities, starters, and any code with a workflow handle.

```python
from temporalio.contrib.pubsub import PubSubClient

client = PubSubClient(workflow_handle, batch_interval=2.0)

# --- Publishing ---
async with client:
    client.publish("events", b'{"type":"TEXT_DELTA","delta":"hello"}')
    client.publish("events", b'{"type":"TEXT_DELTA","delta":" world"}')
    client.publish("events", b'{"type":"TEXT_COMPLETE"}', priority=True)
    # priority=True forces an immediate flush
    # context manager exit flushes remaining buffer

# --- Subscribing ---
async for item in client.subscribe(["events"], from_offset=0):
    print(item.offset, item.topic, item.data)
    if is_done(item):
        break
```

### `PubSubClient` details

| Method | Description |
|---|---|
| `publish(topic, data, priority=False)` | Buffer a message. If `priority=True`, flush immediately. |
| `flush()` | Send all buffered messages to the workflow via signal. |
| `subscribe(topics, from_offset=0)` | Returns an `AsyncIterator[PubSubItem]`. Internally polls via the `__pubsub_poll` update. |
| `get_offset()` | Query the current log offset. |

Constructor parameters:

| Parameter | Default | Description |
|---|---|---|
| `handle` | required | `WorkflowHandle` to the broker workflow. |
| `batch_interval` | `2.0` | Seconds between automatic flushes. |

The client implements `AsyncContextManager`. Entering starts the background flush
timer; exiting cancels it and does a final flush.

### Activity convenience

```python
from temporalio.contrib.pubsub import PubSubClient
from temporalio import activity

async def get_pubsub_client(**kwargs) -> PubSubClient:
    """Create a PubSubClient for the current activity's parent workflow."""
    info = activity.info()
    handle = activity.client().get_workflow_handle(info.workflow_id)
    return PubSubClient(handle, **kwargs)
```

## Data Types

All types use standard Temporal serialization (default data converter) for
cross-language compatibility.

```python
@dataclass
class PubSubItem:
    offset: int       # Global monotonic offset
    topic: str        # Topic string
    data: bytes       # Opaque payload

@dataclass
class PublishInput:
    items: list[PublishEntry]

@dataclass
class PublishEntry:
    topic: str
    data: bytes
    priority: bool = False

@dataclass
class PollInput:
    topics: list[str]       # Filter to these topics (empty = all)
    from_offset: int        # Start reading from this global offset
    timeout: float = 300.0  # Server-side wait timeout

@dataclass
class PollResult:
    items: list[PubSubItem]
    next_offset: int         # Offset for next poll call
```

## Design Decisions

### 1. Topics are plain strings, no hierarchy

Topics are exact-match strings. No prefix matching, no wildcards. A subscriber
provides a list of topic strings to filter on; an empty list means "all topics."

**Rationale**: Simplicity. Prefix matching adds implementation complexity and is
rarely needed for the streaming use cases this targets.

### 2. Items are opaque byte strings

The workflow does not interpret payloads. This enables cross-language
compatibility — each SDK's client serializes/deserializes in its own language.

**Rationale**: The pub/sub layer is transport. Application semantics belong in the
application.

### 3. Global monotonic offsets, not per-topic

Every entry gets a global offset from a single counter. Subscribers filter by topic
but advance through the global offset space.

**Rationale**: Simpler implementation. Global ordering means a subscriber to
multiple topics sees a consistent interleaving. The tradeoff is that a
single-topic subscriber may see gaps in offset numbers — but `next_offset` in
`PollResult` handles continuation cleanly.

### 4. No topic creation

Topics are implicit. Publishing to a topic creates it. Subscribing to a
nonexistent topic returns no items (and waits for new ones).

**Rationale**: Eliminates a management API and lifecycle concerns. Matches the
lightweight "just strings" philosophy.

### 5. Priority forces flush, does not reorder

Setting `priority=True` on a publish causes the client to immediately flush its
buffer. It does NOT reorder items in the log — the priority item appears in its
natural position after any previously-buffered items.

**Rationale**: Reordering would break the append-only log invariant and complicate
offset semantics. The purpose of priority is latency-sensitive delivery (e.g.,
"thinking complete" events), not importance ranking.

### 6. Session ordering

Publications from a single client are ordered. The workflow serializes all signal
processing, so concurrent publishers get a total order (though the interleaving is
nondeterministic). Once items are in the log, their order is stable — reads are
repeatable.

### 7. Batching is built into the client

The `PubSubClient` includes a Nagle-like batcher (buffer + timer). This is the
same pattern as the existing `EventBatcher` but generalized. Batching amortizes
Temporal signal overhead — instead of one signal per token, a 2-second window
batches hundreds of tokens into a single signal.

### 8. Subscription is poll-based, exposed as async iterator

The primitive is `__pubsub_poll` (a Temporal update with `wait_condition`). The
`subscribe()` method wraps this in an `AsyncIterator` that handles polling,
reconnection, and yielding items one at a time.

**Why poll, not push**: Temporal has no server-push to external clients. Updates
with `wait_condition` are the closest thing — the workflow blocks until data is
available, so the client doesn't busy-wait.

**Why async iterator**: Idiomatic Python. Matches what users expect from
Kafka consumers, Redis XREAD, NATS subscriptions, etc.

### 9. Workflow can publish but should not subscribe

Workflow code can call `self.publish()` directly — this is deterministic (appends
to a list). Reading from the log within workflow code is also possible via
`self._pubsub_log` but breaks the failure-free abstraction because:

- External publishers send data via signals, which are non-deterministic inputs
- Branching on signal content creates replay-sensitive code paths

If a workflow needs to react to published data, it should do so in signal handlers,
not by polling its own log.

### 10. Event retention: full log for workflow lifetime (future: snapshot + truncate)

For now, the log grows unbounded for the workflow's lifetime. This is acceptable
for the target use cases (streaming agent sessions lasting minutes to hours).

**Future extension — snapshot + truncate**:

1. `snapshot(topic)` → serialize current subscriber state as a special log entry
2. `truncate(before_offset)` → discard entries before the offset
3. Offsets remain monotonic (never reset)
4. New subscribers start from the snapshot entry
5. Natural integration with `continue_as_new()` — carry the snapshot forward

This follows the event sourcing pattern (snapshot + event replay) and is analogous
to Kafka's log compaction. We note it here as a planned extension but do not
implement it in v1.

## Signal / Update / Query Names

For cross-language interop, the handler names are fixed strings:

| Handler | Temporal name | Kind |
|---|---|---|
| `__pubsub_publish` | `__pubsub_publish` | signal |
| `__pubsub_poll` | `__pubsub_poll` | update |
| `__pubsub_offset` | `__pubsub_offset` | query |

Other language SDKs implementing the same protocol must use these exact names.

## Cross-Language Protocol

Any Temporal client in any language can interact with a pub/sub workflow by:

1. **Publishing**: Send signal `__pubsub_publish` with `PublishInput` payload
2. **Subscribing**: Execute update `__pubsub_poll` with `PollInput`, loop
3. **Checking offset**: Query `__pubsub_offset`

The payload types are simple composites of strings, bytes, ints, and bools — all
representable in every Temporal SDK's default data converter.

## File Layout

```
temporalio/contrib/pubsub/
├── __init__.py          # Public API exports
├── _mixin.py            # PubSubMixin (workflow-side)
├── _client.py           # PubSubClient (external-side, includes batcher)
├── _types.py            # Shared data types
└── README.md            # Usage documentation
```

## Local Development

To use the local sdk-python with temporal-streaming-agents-samples:

```toml
# In temporal-streaming-agents-samples/backend-temporal/pyproject.toml
[tool.uv.sources]
temporalio = { path = "../../../sdk-python", editable = true }
```

This requires `maturin develop` to have been run at least once (for the Rust
bridge), but subsequent Python-only changes are reflected immediately.

## Migration Plan (temporal-streaming-agents-samples)

The existing streaming code maps directly to the new contrib:

| Current code | Replaces with |
|---|---|
| `EventBatcher` | `PubSubClient` (with batching) |
| `receive_events` signal | `__pubsub_publish` signal (from mixin) |
| `poll_events` update | `__pubsub_poll` update (from mixin) |
| `get_event_count` query | `__pubsub_offset` query (from mixin) |
| `_event_list` state | `PubSubMixin._pubsub_log` |
| `_get_batcher()` helper | `get_pubsub_client()` or `PubSubClient(handle)` |
| `ActivityEventsInput` | `PublishInput` |
| `PollEventsInput/Result` | `PollInput/PollResult` |
