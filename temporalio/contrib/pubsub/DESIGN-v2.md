# Temporal Workflow Pub/Sub — Design Document v2

Consolidated design document reflecting the current implementation.
Supersedes [DESIGN.md](./DESIGN.md) and its addenda
([CAN](./DESIGN-ADDENDUM-CAN.md), [Topics](./DESIGN-ADDENDUM-TOPICS.md),
[Dedup](./DESIGN-ADDENDUM-DEDUP.md)), which are preserved as historical
records of the design exploration.

## Overview

A reusable pub/sub module for Temporal workflows. The workflow acts as the
message broker — it holds an append-only log of `(topic, data)` entries.
External clients (activities, starters, other services) publish and subscribe
through the workflow handle using Temporal primitives (signals, updates,
queries).

The module ships as `temporalio.contrib.pubsub` in the Python SDK and is
designed to be cross-language compatible. Payloads are opaque byte strings —
the workflow does not interpret them.

## Architecture

```
                    ┌──────────────────────────────────┐
                    │         Temporal Workflow         │
                    │          (PubSubMixin)            │
                    │                                   │
                    │  ┌─────────────────────────────┐  │
                    │  │   Append-only log            │  │
                    │  │   [(topic, data), ...]       │  │
                    │  │   base_offset: int           │  │
                    │  │   publisher_sequences: {}    │  │
                    │  └─────────────────────────────┘  │
                    │                                   │
  signal ──────────►│  __pubsub_publish (with dedup)    │
  update ──────────►│  __pubsub_poll (long-poll)        │◄── subscribe()
  query  ──────────►│  __pubsub_offset                  │
                    │                                   │
                    │  publish() ── workflow-side        │
                    └──────────────────────────────────┘
                              │
                              │ continue-as-new
                              ▼
                    ┌──────────────────────────────────┐
                    │  PubSubState carries:             │
                    │    log, base_offset,              │
                    │    publisher_sequences            │
                    └──────────────────────────────────┘
```

## API Surface

### Workflow side — `PubSubMixin`

A mixin class that adds signal, update, and query handlers to any workflow.

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

Call `init_pubsub()` in `__init__` for fresh workflows. When accepting
continue-as-new state, call it in `run()` with the `prior_state` argument
(see [Continue-as-New](#continue-as-new)).

| Method / Handler | Kind | Description |
|---|---|---|
| `init_pubsub(prior_state=None)` | instance method | Initialize internal state. Must be called before use. |
| `publish(topic, data)` | instance method | Append to the log from workflow code. |
| `get_pubsub_state(publisher_ttl=900)` | instance method | Snapshot for CAN. Prunes dedup entries older than TTL. |
| `drain_pubsub()` | instance method | Unblock polls and reject new ones for CAN. |
| `truncate_pubsub(up_to_offset)` | instance method | Discard log entries before offset. |
| `__pubsub_publish` | `@workflow.signal` | Receives publications from external clients (with dedup). |
| `__pubsub_poll` | `@workflow.update` | Long-poll subscription: blocks until new items or drain. |
| `__pubsub_offset` | `@workflow.query` | Returns the current global offset. |

### Client side — `PubSubClient`

Used by activities, starters, and any code with a workflow handle.

```python
from temporalio.contrib.pubsub import PubSubClient

# Preferred: factory method (enables CAN following + activity auto-detect)
client = PubSubClient.create(temporal_client, workflow_id)

# --- Publishing (with batching) ---
async with client:
    client.publish("events", b'{"type":"TEXT_DELTA","delta":"hello"}')
    client.publish("events", b'{"type":"TEXT_DELTA","delta":" world"}')
    client.publish("events", b'{"type":"TEXT_COMPLETE"}', priority=True)

# --- Subscribing ---
async for item in client.subscribe(["events"], from_offset=0):
    print(item.topic, item.data)
    if is_done(item):
        break
```

| Method | Description |
|---|---|
| `PubSubClient.create(client?, wf_id?)` | Factory (preferred). Auto-detects activity context if args omitted. |
| `PubSubClient(handle)` | From handle directly (no CAN following). |
| `publish(topic, data, priority=False)` | Buffer a message. Priority triggers immediate flush (fire-and-forget). |
| `subscribe(topics, from_offset, poll_cooldown=0.1)` | Async iterator. Always follows CAN chains when created via `create`. |
| `get_offset()` | Query current global offset. |

Use as `async with` for batched publishing with automatic flush on exit.
There is no public `flush()` method — use `priority=True` on `publish()`
for immediate delivery, or rely on the background flusher and context
manager exit flush.

#### Activity convenience

When called from within an activity, `client` and `workflow_id` can be
omitted from `create()` — they are inferred from the activity context:

```python
@activity.defn
async def stream_events() -> None:
    client = PubSubClient.create(batch_interval=2.0)
    async with client:
        for chunk in generate_chunks():
            client.publish("events", chunk)
            activity.heartbeat()
```

## Data Types

```python
@dataclass
class PubSubItem:
    topic: str        # Topic string
    data: bytes       # Opaque payload

@dataclass
class PublishEntry:
    topic: str
    data: bytes

@dataclass
class PublishInput:
    items: list[PublishEntry]
    publisher_id: str = ""     # For exactly-once dedup
    sequence: int = 0          # Monotonic per publisher

@dataclass
class PollInput:
    topics: list[str]          # Filter (empty = all)
    from_offset: int = 0       # Global offset to resume from
    timeout: float = 300.0     # Server-side wait timeout

@dataclass
class PollResult:
    items: list[PubSubItem]
    next_offset: int = 0       # Offset for next poll

class PubSubState(BaseModel):  # Pydantic for CAN round-tripping
    log: list[PubSubItem] = []
    base_offset: int = 0
    publisher_sequences: dict[str, int] = {}
    publisher_last_seen: dict[str, float] = {}  # For TTL pruning
```

`PubSubItem` does not carry an offset field. The global offset is derived
from the item's position in the log plus `base_offset`. It is exposed only
through `PollResult.next_offset` and the `__pubsub_offset` query.

`PubSubState` is a Pydantic model (not a dataclass) so that Pydantic-based
data converters can properly reconstruct it through continue-as-new. The
containing workflow input must type the field as `PubSubState | None`, not
`Any` — Pydantic deserializes `Any` fields as plain dicts, losing the type.

## Design Decisions

### 1. Topics are plain strings, no hierarchy

Topics are exact-match strings. No prefix matching, no wildcards. A subscriber
provides a list of topic strings to filter on; an empty list means "all topics."

### 2. Items are opaque byte strings

The workflow does not interpret payloads. This enables cross-language
compatibility. The pub/sub layer is transport; application semantics belong
in the application.

### 3. Global offsets, NATS JetStream model

Every entry gets a global offset from a single counter. Subscribers filter by
topic but advance through the global offset space.

We surveyed offset models across Kafka, Redis Streams, NATS JetStream, PubNub,
Google Pub/Sub, RabbitMQ Streams, and Amazon SQS/SNS. No major system provides
a true global offset across independent topics. The two closest:

- **NATS JetStream**: one stream captures multiple subjects via wildcards, with
  a single sequence counter. This is our model.
- **PubNub**: wall-clock nanosecond timestamp as cursor across channels.

We evaluated six alternatives for handling the information leakage that global
offsets create (a single-topic subscriber can infer other-topic activity from
gaps): per-topic counts, opaque cursors, encrypted cursors, per-topic lists,
per-topic offsets with cursor hints, and accepting the leakage. See
[DESIGN-ADDENDUM-TOPICS.md](./DESIGN-ADDENDUM-TOPICS.md) for the full
analysis.

**Decision:** Global offsets are the right choice for workflow-scoped pub/sub.
The subscriber is the BFF — trusted server-side code. Information leakage is
contained at the BFF trust boundary, which assigns its own gapless SSE event
IDs to the browser. The global offset never reaches the end client.

### 4. No topic creation

Topics are implicit. Publishing to a topic creates it. Subscribing to a
nonexistent topic returns no items and waits for new ones.

### 5. Priority forces flush, does not reorder

`priority=True` causes the client to immediately flush its buffer. It does NOT
reorder items — the priority item appears in its natural position after any
previously-buffered items. The purpose is latency-sensitive delivery, not
importance ranking.

### 6. Session ordering

Publications from a single client are ordered. The workflow serializes all
signal processing, so concurrent publishers get a total order (though the
interleaving is nondeterministic). Once items are in the log, their order is
stable — reads are repeatable.

### 7. Batching is built into the client

`PubSubClient` includes a Nagle-like batcher (buffer + timer). The async
context manager starts a background flush task; exiting cancels it and does a
final flush. Batching amortizes Temporal signal overhead.

Parameters:
- `batch_interval` (default 2.0s): timer between automatic flushes.
- `max_batch_size` (optional): auto-flush when buffer reaches this size.

### 8. Subscription is poll-based, exposed as async iterator

The primitive is `__pubsub_poll` (a Temporal update with `wait_condition`).
`subscribe()` wraps this in an `AsyncIterator` with a configurable
`poll_interval` (default 0.1s) to rate-limit polls.

Temporal has no server-push to external clients. Updates with `wait_condition`
are the closest thing — the workflow blocks until data is available.

### 9. Workflow can publish but should not subscribe

Workflow code can call `self.publish()` directly — this is deterministic.
Reading from the log within workflow code is possible but breaks the
failure-free abstraction because external publishers send data via signals
(non-deterministic inputs), and branching on signal content creates
replay-sensitive code paths.

### 10. `base_offset` for future truncation

The log carries a `base_offset` (0 today). All offset arithmetic uses
`offset - base_offset` to index into the log. This supports future log
truncation: discard a prefix of consumed entries, advance `base_offset`,
and global offsets remain monotonic. If `offset < base_offset`, the
subscriber has fallen behind truncation — the poll raises an error.

Truncation is deferred to a future iteration. Until then, the log grows
without bound within a run and is compacted only through continue-as-new.

## Exactly-Once Publish Delivery

External publishers get exactly-once delivery through publisher ID + sequence
number deduplication, following the Kafka producer model.

### Problem

`flush()` sends items via a Temporal signal. If the signal call raises after
the server accepted it (e.g., network timeout on the response), the client
cannot distinguish delivered from not-delivered. Without dedup, the client
must choose between at-most-once (data loss) and at-least-once (silent
duplication).

### Solution

Each `PubSubClient` instance generates a UUID (`publisher_id`) on creation.
Each `flush()` increments a monotonic `sequence` counter. The signal payload
includes both. The workflow tracks the highest seen sequence per publisher in
`_publisher_sequences: dict[str, int]` and rejects any signal with
`sequence <= last_seen`.

```
Client                              Workflow
  │                                    │
  │  signal(publisher_id, seq=1)       │
  │───────────────────────────────────►│ seq 1 > 0 → accept, record seq=1
  │                                    │
  │  signal(publisher_id, seq=1)       │  (retry after timeout)
  │───────────────────────────────────►│ seq 1 <= 1 → reject (duplicate)
  │                                    │
  │  signal(publisher_id, seq=2)       │
  │───────────────────────────────────►│ seq 2 > 1 → accept, record seq=2
```

### Client-side flush (TLA+-verified algorithm)

The flush algorithm has been formally verified using TLA+ model checking.
See `verification/PROOF.md` for the full correctness proof and
`verification/PubSubDedup.tla` for the spec.

```python
async def _flush(self) -> None:
    async with self._flush_lock:
        if self._pending is not None:
            # Retry failed batch with same sequence
            batch = self._pending
            seq = self._pending_seq
        elif self._buffer:
            # New batch
            seq = self._sequence + 1
            batch = self._buffer
            self._buffer = []
            self._pending = batch
            self._pending_seq = seq
        else:
            return
        try:
            await self._handle.signal(
                "__pubsub_publish",
                PublishInput(items=batch, publisher_id=self._publisher_id,
                             sequence=seq),
            )
            self._sequence = seq     # advance confirmed sequence
            self._pending = None     # clear pending
        except Exception:
            pass                     # pending stays for retry
            raise
```

- **Separate pending from buffer**: failed batches stay in `_pending`, not
  restored to `_buffer`. New `publish()` calls during retry go to the fresh
  buffer. This prevents the data-loss bug where items would be merged into a
  retry batch under a different sequence number.
- **Retry with same sequence**: on failure, the next `_flush()` retries the
  same `_pending` with the same `_pending_seq`. If the signal was delivered
  but the client saw an error, the workflow deduplicates the retry.
- **Sequence advances only on success**: `_sequence` (confirmed) is updated
  only after the signal call returns without error.
- **Lock for coalescing**: concurrent `_flush()` callers queue on the lock.
- **max_retry_duration**: if set, the client gives up retrying after this
  duration and raises `TimeoutError`. Must be less than the workflow's
  `publisher_ttl` to preserve exactly-once guarantees.

### Dedup state and TTL pruning

`publisher_sequences` is `dict[str, int]` — bounded by number of publishers
(typically 1-2), not number of flushes. Carried through continue-as-new in
`PubSubState`. If `publisher_id` is empty (workflow-internal publish or legacy
client), dedup is skipped.

`publisher_last_seen` tracks the last `workflow.time()` each publisher was
seen. During `get_pubsub_state(publisher_ttl=900)`, entries older than TTL
are pruned to bound memory across long-lived workflow chains.

**Safety constraint**: `publisher_ttl` must exceed the client's
`max_retry_duration`. If a publisher's dedup entry is pruned while it still
has a pending retry, the retry could be accepted as new, creating duplicates.
This is formally verified in `verification/PubSubDedupTTL.tla` — TLC finds
the counterexample for unsafe pruning and confirms safe pruning preserves
NoDuplicates.

## Continue-as-New

### Problem

The pub/sub mixin accumulates workflow history through signals (each
`__pubsub_publish`) and updates (each `__pubsub_poll` response). Over a
streaming session, history grows toward the ~50K event threshold. CAN resets
the history while carrying the canonical log copy forward.

### State

```python
class PubSubState(BaseModel):
    log: list[PubSubItem] = []
    base_offset: int = 0
    publisher_sequences: dict[str, int] = {}
    publisher_last_seen: dict[str, float] = {}
```

`init_pubsub(prior_state)` restores all four fields. `get_pubsub_state()`
snapshots them.

### Draining

A long-poll `__pubsub_poll` can block for up to 300 seconds. To allow CAN to
proceed, draining uses two mechanisms:

1. **`drain_pubsub()`** sets a flag that unblocks all waiting poll handlers
   (the `or self._pubsub_draining` clause in `wait_condition`).
2. **Update validator** rejects new polls when draining, so no new handlers
   start and `all_handlers_finished()` stabilizes.

```python
# CAN sequence in the parent workflow:
self.drain_pubsub()
await workflow.wait_condition(workflow.all_handlers_finished)
workflow.continue_as_new(args=[WorkflowInput(
    pubsub_state=self.get_pubsub_state(),
)])
```

### Client-side CAN following

`subscribe()` always follows CAN chains when the client was created via
`for_workflow()`. When a poll fails with
`WorkflowUpdateRPCTimeoutOrCancelledError`, the client calls `describe()` on
the handle. If the status is `CONTINUED_AS_NEW`, it gets a fresh handle for
the same workflow ID (targeting the latest run) and retries the poll from the
same offset.

```python
async def _follow_continue_as_new(self) -> bool:
    if self._client is None:
        return False
    try:
        desc = await self._handle.describe()
    except Exception:
        return False
    if desc.status == WorkflowExecutionStatus.CONTINUED_AS_NEW:
        self._handle = self._client.get_workflow_handle(self._workflow_id)
        return True
    return False
```

The `describe()` check prevents infinite loops: if the workflow completed or
failed (not CAN), the subscriber stops instead of retrying.

### Offset continuity

Since the full log is carried forward:

- Pre-CAN: offsets `0..N-1`, log length N.
- Post-CAN: `init_pubsub(prior_state)` restores N items. New appends start
  at offset N.
- A subscriber at offset K resumes seamlessly against the new run.

### Edge cases

**Payload size limit.** The full log in CAN input could approach Temporal's
2 MB limit for very long sessions. Mitigation: truncation (discarding consumed
entries before CAN) is the natural extension, supported by `base_offset`.

**Signal delivery during CAN.** A publisher sending mid-CAN may get errors if
its handle is pinned to the old run. The workflow should ensure activities
complete before triggering CAN.

**Concurrent subscribers.** Each maintains its own offset. Sharing a
`PubSubClient` across concurrent `subscribe()` calls is safe.

## Information Leakage and the BFF

Global offsets leak cross-topic activity (a single-topic subscriber sees gaps).
This is acceptable within the pub/sub API because the subscriber is the BFF —
trusted server-side code.

The BFF contains the leakage by assigning its own gapless SSE event IDs:

```python
start_offset = await pubsub.get_offset()
async for item in pubsub.subscribe(topics=["events"], from_offset=start_offset):
    yield sse_event(item, id=next_sse_id())
```

The browser sees `id: 1`, `id: 2`, `id: 3`. On reconnect, `Last-Event-ID`
maps back to a global offset at the BFF layer.

## Cross-Language Protocol

Any Temporal client in any language can interact with a pub/sub workflow by:

1. **Publishing**: Signal `__pubsub_publish` with `PublishInput` payload
2. **Subscribing**: Execute update `__pubsub_poll` with `PollInput`, loop
3. **Checking offset**: Query `__pubsub_offset`

Double-underscore prefix on handler names avoids collisions with application
signals/updates. The payload types are simple composites of strings, bytes,
and ints — representable in every Temporal SDK's default data converter.

## File Layout

```
temporalio/contrib/pubsub/
├── __init__.py                  # Public API exports
├── _mixin.py                    # PubSubMixin (workflow-side)
├── _client.py                   # PubSubClient (external-side)
├── _types.py                    # Shared data types
├── README.md                    # Usage documentation
├── DESIGN-v2.md                 # This document
├── DESIGN.md                    # Historical: original design
├── DESIGN-ADDENDUM-CAN.md       # Historical: CAN exploration
├── DESIGN-ADDENDUM-TOPICS.md    # Historical: offset model exploration
├── DESIGN-ADDENDUM-DEDUP.md     # Historical: dedup exploration
└── verification/                # TLA+ formal verification
    ├── README.md                # Overview and running instructions
    ├── PROOF.md                 # Full correctness proof
    ├── PubSubDedup.tla          # Correct single-publisher protocol
    ├── PubSubDedupInductive.tla # Inductive invariant (unbounded proof)
    ├── PubSubDedupTTL.tla       # Multi-publisher + TTL pruning
    └── PubSubDedupBroken.tla    # Old (broken) algorithm — counterexample
```
