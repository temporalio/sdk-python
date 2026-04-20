# Temporal Workflow Pub/Sub — Design Document v2

Consolidated design document reflecting the current implementation.

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
                    │         Temporal Workflow        │
                    │          (PubSubMixin)           │
                    │                                  │
                    │  ┌────────────────────────────┐  │
                    │  │   Append-only log          │  │
                    │  │   [(topic, data), ...]     │  │
                    │  │   base_offset: int         │  │
                    │  │   publisher_sequences: {}  │  │
                    │  └────────────────────────────┘  │
                    │                                  │
  signal ──────────►│  __pubsub_publish (with dedup)   │
  update ──────────►│  __pubsub_poll (long-poll)       │◄── subscribe()
  query  ──────────►│  __pubsub_offset                 │
                    │                                  │
                    │  publish() ── workflow-side      │
                    └──────────────────────────────────┘
                              │
                              │ continue-as-new
                              ▼
                    ┌──────────────────────────────────┐
                    │  PubSubState carries:            │
                    │    log, base_offset,             │
                    │    publisher_sequences           │
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

@dataclass
class PollResult:
    items: list[PubSubItem]
    next_offset: int = 0       # Offset for next poll

@dataclass
class PubSubState:
    log: list[PubSubItem] = field(default_factory=list)
    base_offset: int = 0
    publisher_sequences: dict[str, int] = field(default_factory=dict)
    publisher_last_seen: dict[str, float] = field(default_factory=dict)  # For TTL pruning
```

`PubSubItem` does not carry an offset field. The global offset is derived
from the item's position in the log plus `base_offset`. It is exposed only
through `PollResult.next_offset` and the `__pubsub_offset` query.

The containing workflow input must type the field as `PubSubState | None`,
not `Any` — `Any`-typed fields deserialize as plain dicts, losing the type.

## Design Decisions

### 1. Topics are plain strings, no hierarchy

Topics are exact-match strings. No prefix matching, no wildcards. A subscriber
provides a list of topic strings to filter on; an empty list means "all topics."

### 2. Items are opaque byte strings

The workflow does not interpret payloads. This enables cross-language
compatibility. The pub/sub layer is transport; application semantics belong
in the application.

The alternative is typed payloads — the pub/sub layer accepts
application-defined types and uses Temporal's data converter for
serialization. We chose opaque bytes because:

1. **Decoupling.** Different publishers on the same workflow may publish
   different types to different topics. Opaque bytes let each publisher
   choose its own serialization.
2. **Layering.** The data converter already handles the wire format of
   `PublishInput` and `PollResult` (the signal/update envelopes). Using it
   for payload data would mean the converter runs at two levels.
3. **Type hints.** `DataConverter.decode()` requires a target type. The
   pub/sub layer does not know the application's types, so subscribers would
   need to declare expected types per topic — complexity the application
   handles trivially with `json.loads()`.

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
per-topic offsets with cursor hints, and accepting the leakage.

**Decision:** Global offsets are the right choice for workflow-scoped pub/sub.

**Why not per-topic offsets?** The most sophisticated alternative — per-topic
offsets with opaque cursors carrying global position hints — was rejected
for three reasons:

1. **The threat model doesn't apply.** Information leakage assumes untrusted
   multi-tenant subscribers who shouldn't learn about each other's traffic
   volumes. That's Kafka's world — separate consumers for separate services.
   In workflow-scoped pub/sub, the subscriber is the BFF: trusted server-side
   code that could just as easily subscribe to all topics.

2. **Cursor portability.** A global offset is a stream position that works
   regardless of which topics you filter on. You can subscribe to `["events"]`,
   then later subscribe to `["events", "thinking"]` with the same offset.
   Per-topic cursors are coupled to the filter — you need a separate cursor per
   topic, and adding a topic to your subscription requires starting it from the
   beginning.

3. **Unjustified complexity.** Per-topic cursors require cursor
   parsing/formatting, a `topic_counts` dict that survives continue-as-new, a
   multi-cursor alignment algorithm, and stale-hint fallback paths. For log
   sizes of thousands of items where a filtered slice is microseconds, this
   machinery adds cost without measurable benefit.

**Leakage is contained at the BFF trust boundary.** The global offset stays
between workflow and BFF. The BFF assigns its own gapless SSE event IDs to the
browser. The global offset never reaches the end client. See
[Information Leakage and the BFF](#information-leakage-and-the-bff) for the
full mechanism.

### 4. No topic creation

Topics are implicit. Publishing to a topic creates it. Subscribing to a
nonexistent topic returns no items and waits for new ones.

### 5. Priority forces flush, does not reorder

`priority=True` causes the client to immediately flush its buffer. It does NOT
reorder items — the priority item appears in its natural position after any
previously-buffered items. The purpose is latency-sensitive delivery, not
importance ranking.

### 6. Session ordering

Publications from a single client are ordered. This relies on two Temporal
guarantees:

> "Signals are delivered in the order they are received by the Cluster and
> written to History."
> ([docs](https://docs.temporal.io/workflows#signal))

Specifically: (1) signals sent sequentially from the same client appear in
workflow history in send order, and (2) signal handlers are invoked in
history order. The guarantee breaks down only for *concurrent* signals — if
two signal RPCs are in flight simultaneously, their order in history is
nondeterministic. The `PubSubClient` flush lock (`_flush_lock`) ensures
signals are never in flight concurrently from a single client:

1. Acquire lock
2. `await handle.signal(...)` — blocks until server writes to history
3. Release lock

Combined with the workflow's single-threaded signal processing (the
`__pubsub_publish` handler is synchronous — no `await`), items within and
across batches from a single publisher preserve their publish order.

Concurrent publishers get a total order in the log (the workflow serializes
all signal processing), but the interleaving is nondeterministic — it depends
on arrival order at the server. Per-publisher ordering is preserved. This is
formally verified as `OrderPreservedPerPublisher`.

Once items are in the log, their order is stable — reads are repeatable.

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

**Poll efficiency.** The poll slices `self._pubsub_log[from_offset - base_offset:]`
and filters by topic. The common case — single topic, continuing from last
poll — is O(new items since last poll). The global offset points directly to
the resume position with no scanning or cursor alignment. Multi-topic polls
are the same cost: one slice, one filter pass. The worst case is a poll from
offset 0 (full log scan), which only happens on first connection or after the
subscriber falls behind.

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

### 11. No timeout on long-poll

`wait_condition` in the poll handler has no timeout. The poll blocks
indefinitely until one of three things happens:

1. **New data arrives** — the `len(log) > offset` condition fires.
2. **Draining for continue-as-new** — `drain_pubsub()` sets the flag.
3. **Client disconnects** — the BFF drops the SSE connection, cancels the
   update RPC, and the handler becomes an inert coroutine cleaned up at
   the next drain cycle.

A previous design used a 5-minute timeout as a defensive "don't block
forever" mechanism. This was removed because:

- **It adds unnecessary history events.** Every poll creates a `TimerStarted`
  event. For a streaming session doing hundreds of polls, this doubles the
  history event count and accelerates approach to the ~50K event CAN threshold.
- **The drain mechanism already handles cleanup.** `drain_pubsub()` unblocks
  all waiting polls, and the update validator rejects new polls, so
  `all_handlers_finished()` converges without timers.
- **Zombie polls are harmless.** If a client crashes without cancelling, its
  poll handler is just an in-memory coroutine waiting on a condition. It
  consumes no Temporal actions and is cleaned up at the next CAN cycle.

### 12. Signals for publish, updates for poll

Publishing uses signals (fire-and-forget); subscription uses updates
(request-response with `wait_condition`). These choices are deliberate.

**Why signals for publish:**

- **Non-blocking flush.** The activity can buffer tokens at whatever rate
  the LLM produces them. `handle.signal(...)` enqueues at the server and
  returns immediately — the publisher is never throttled by the workflow's
  processing speed.
- **Lower history cost.** Each signal adds 1 event (`WorkflowSignalReceived`).
  An update adds 2 (`UpdateAccepted` + `UpdateCompleted`). For a streaming
  session with hundreds of flushes, signals halve the history growth rate and
  delay the CAN threshold.
- **No concurrency limits.** Temporal Cloud enforces per-workflow update
  limits. Signals have no equivalent limit, making them safer for
  high-throughput publishing.

**Why updates for poll:**

- The caller needs a result (the items). Blocking is the desired behavior
  (long-poll semantics). `wait_condition` inside an update handler is the
  natural fit.

**Why not updates for publish?** The main attraction would be platform-native
exactly-once via Update ID, eliminating application-level dedup. However:

1. Update ID dedup does not persist across continue-as-new. For CAN workflows,
   application-level dedup is required regardless
   ([temporal/temporal#6375](https://github.com/temporalio/temporal/issues/6375)).
2. Each flush would block for a round-trip to the worker (~10-50ms), throttling
   the publisher.
3. The 2x history cost accelerates approach to the CAN threshold.

If the cross-CAN dedup gap is fixed and backpressure becomes desirable,
switching publish to updates is a mechanical change — the dedup protocol,
dedup protocol, and mixin handler logic are unchanged.

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

### Client-side flush

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
`PubSubState`. If `publisher_id` is empty (workflow-internal publish),
dedup is skipped.

`publisher_last_seen` tracks the last `workflow.time()` each publisher was
seen. During `get_pubsub_state(publisher_ttl=900)`, entries older than TTL
are pruned to bound memory across long-lived workflow chains.

**Safety constraint**: `publisher_ttl` must exceed the client's
`max_retry_duration`. If a publisher's dedup entry is pruned while it still
has a pending retry, the retry could be accepted as new, creating duplicates.

### Scope: what pub/sub dedup does and does not handle

Duplicates arise at three points in the pipeline. Each layer handles the
duplicates it introduces — applying the end-to-end principle (Saltzer, Reed,
Clark 1984).

```
LLM API  -->  Activity  -->  PubSubClient  -->  Workflow Log  -->  BFF/SSE  -->  Browser
  (A)                            (B)                                (C)
```

| Type | Cause | Handled by |
|---|---|---|
| A: Duplicate LLM work | Activity retry produces a second, semantically equivalent but textually different response | Application layer (activity idempotency keys, workflow orchestration) |
| B: Duplicate signal batches | Signal retry after ambiguous failure delivers the same `(publisher_id, sequence)` batch twice | **Pub/sub layer** (`sequence <= last_seen` rejection) |
| C: Duplicate SSE events | Browser reconnects and BFF replays previously-delivered events | Delivery layer (SSE `Last-Event-ID`, idempotent frontend reducers) |

**Why Type A doesn't belong here.** Data escapes to the subscriber during the
first LLM call — tokens are consumed, forwarded to the browser, and rendered
before any retry occurs. By the time a retry produces a duplicate response,
the original is already consumed. The pub/sub layer has no opportunity to
suppress it, and resolution requires application semantics (discard, replace,
merge) that the transport layer has no knowledge of.

**Why Type B must be here.** The consumer sees `PubSubItem(topic, data)` with
no unique ID. If the workflow accepted a duplicate batch, the duplicates would
get fresh offsets and be indistinguishable from originals. Content-based dedup
has false positives (an LLM legitimately produces the same token twice; a
status event like `{"type":"THINKING_START"}` repeats across turns). The
`(publisher_id, sequence)` check is the only correct implementation — it
preserves transport encapsulation and uses context only the transport layer
has.

**Why Type C doesn't belong here.** SSE reconnection is below the pub/sub
layer. The BFF assigns gapless event IDs and maps `Last-Event-ID` back to
global offsets (see [Information Leakage and the BFF](#information-leakage-and-the-bff)).

## Continue-as-New

### Problem

The pub/sub mixin accumulates workflow history through signals (each
`__pubsub_publish`) and updates (each `__pubsub_poll` response). Over a
streaming session, history grows toward the ~50K event threshold. CAN resets
the history while carrying the canonical log copy forward.

### State

```python
@dataclass
class PubSubState:
    log: list[PubSubItem] = field(default_factory=list)
    base_offset: int = 0
    publisher_sequences: dict[str, int] = field(default_factory=dict)
    publisher_last_seen: dict[str, float] = field(default_factory=dict)
```

`init_pubsub(prior_state)` restores all four fields. `get_pubsub_state()`
snapshots them.

### Draining

A long-poll `__pubsub_poll` blocks indefinitely until new data arrives. To
allow CAN to proceed, draining uses two mechanisms:

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
trusted server-side code. The leakage must not reach the end client (browser).

### The problem

If the BFF forwarded `PollResult.next_offset` to the browser (e.g., as an SSE
reconnection cursor), the browser could observe gaps and infer activity on
topics it is not subscribed to. Even if the offset is "opaque," a monotonic
integer with gaps is trivially inspectable.

### Options considered

We evaluated four approaches for browser-side reconnection:

1. **BFF tracks the cursor server-side.** The BFF maintains a per-session
   `session_id → last_offset` mapping. The browser reconnects with just the
   session ID. On BFF restart, cursors are lost — fall back to replaying from
   turn start.

2. **Opaque token from the BFF.** The BFF wraps the global offset in an
   encoded or encrypted token. The browser passes it back on reconnect.
   `base64(offset)` is trivially reversible (security theater); real encryption
   needs a key and adds a layer for marginal benefit over option 1.

3. **BFF assigns SSE event IDs with `Last-Event-ID`.** The BFF emits SSE
   events with `id: 1`, `id: 2`, `id: 3` (a BFF-local counter per stream).
   On reconnect, the browser sends `Last-Event-ID` (built into the SSE spec).
   The BFF maps that back to a global offset internally.

4. **No mid-stream resume.** Browser reconnects, BFF replays from start of
   the current turn. Frontend deduplicates. Simplest, but replays more data
   than necessary.

### Decision: SSE event IDs (option 3)

The BFF assigns gapless integer IDs to SSE events and maintains a small
mapping from SSE event index to global offset. The browser never sees the
workflow's offset — it sees the BFF's event numbering.

```python
sse_id = 0
sse_id_to_offset: dict[int, int] = {}

start_offset = await pubsub.get_offset()
async for item in pubsub.subscribe(topics=["events"], from_offset=start_offset):
    sse_id += 1
    sse_id_to_offset[sse_id] = item_global_offset
    yield f"id: {sse_id}\ndata: {item.data}\n\n"
```

On reconnect, the browser sends `Last-Event-ID: 47`. The BFF looks up the
corresponding global offset and resumes the subscription from there.

The BFF is already per-session and stateful (it holds the SSE connection).
The `sse_id → global_offset` mapping is negligible additional state. On BFF
restart, the mapping is lost — fall back to replaying from turn start (option
4), which is acceptable because agent turns produce modest event volumes and
the frontend reducer is idempotent.

This uses the SSE spec as designed: `Last-Event-ID` exists for exactly this
reconnection pattern.

## Cross-Language Protocol

Any Temporal client in any language can interact with a pub/sub workflow by:

1. **Publishing**: Signal `__pubsub_publish` with `PublishInput` payload
2. **Subscribing**: Execute update `__pubsub_poll` with `PollInput`, loop
3. **Checking offset**: Query `__pubsub_offset`

Double-underscore prefix on handler names avoids collisions with application
signals/updates. The envelope types are simple composites of strings, bytes,
and ints — representable in every Temporal SDK's default data converter.

**Requires the default (JSON) data converter.** The wire protocol depends on
all participants — workflow, publishers, and subscribers — using the default
JSON data converter. A custom converter (protobuf, encryption codecs) would
change how the envelope types serialize, breaking cross-language interop.
This is also why payload data is opaque bytes: the pub/sub layer controls the
envelope format (guaranteed JSON-safe), while the application controls payload
serialization independently.

## Compatibility

The wire protocol evolves under four rules to prevent accidental breakage by
future contributors.

### 1. Additive-only wire evolution

New fields on `PublishInput`, `PollInput`, `PollResult`, and `PubSubState` must
have defaults. Existing field semantics must not change. Temporal's JSON data
converter drops unknown fields on deserialization and uses defaults for missing
fields, so additive changes are safe in both directions (new client → old
workflow, and vice versa). This is the same model as Protocol Buffers wire
compatibility.

### 2. Handler names are immutable

`__pubsub_publish`, `__pubsub_poll`, and `__pubsub_offset` will never change
meaning. If a future change is incompatible with additive evolution, the correct
mechanism is a new handler name (e.g., `__pubsub_v2_poll`) — creating an
entirely separate protocol surface so old and new code never interact.

### 3. `PubSubState` must be forward-compatible

New fields use `field(default_factory=...)` or scalar defaults. Old state loaded
into new code works (new fields get defaults). New state loaded into old code
works (unknown fields dropped by the JSON deserializer). This ensures seamless
continue-as-new across mixed-version deployments.

### 4. No application-level version negotiation

We do not add version fields to payloads, and we do not negotiate protocol
versions between client and workflow. The reasons:

- **Signals cannot return errors.** A version field that the workflow checks on a
  signal creates silent data loss: the workflow rejects the signal, but the
  client (which used fire-and-forget delivery) never learns it was rejected.
  This is strictly worse than the current behavior, where unknown fields are
  harmlessly ignored.
- **Temporal Worker Versioning handles the hard cases.** For a true breaking
  change, deploy the new mixin on a new Build ID. Old running workflows continue
  on old workers; new workflows start on new workers. This operates at the
  infrastructure level — handling in-flight workflows, replay, and mixed-version
  fleets — which message-level version fields cannot.
- **`workflow.patched()` handles in-workflow transitions.** If a new mixin
  version changes behavior (e.g., how it processes a signal), `patched()` gates
  old vs. new logic within the same workflow code during the transition period.

### Field defaults

All fields follow rule 1:

| Field | Default | Behavior when absent |
|---|---|---|
| `PublishInput.publisher_id` | `""` | Empty string skips dedup |
| `PublishInput.sequence` | `0` | Zero skips dedup |
| `_WireItem.offset` | `0` | Zero means "unknown" |
| `PollResult.more_ready` | `False` | No truncation signaled |
| `PubSubState.publisher_last_seen` | `{}` | No TTL pruning state |

## File Layout

```
temporalio/contrib/pubsub/
├── __init__.py                  # Public API exports
├── _mixin.py                    # PubSubMixin (workflow-side)
├── _client.py                   # PubSubClient (external-side)
├── _types.py                    # Shared data types
├── README.md                    # Usage documentation
└── DESIGN-v2.md                 # This document
```
