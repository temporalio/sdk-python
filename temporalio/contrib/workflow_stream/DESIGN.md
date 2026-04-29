# Temporal Workflow Streams — Design Document

Consolidated design document reflecting the current implementation.

> The Python code in `sdk-python/temporalio/contrib/workflow_stream/` is authoritative.
> Both this document and the Notion page
> ["Streaming API Design Considerations"](https://www.notion.so/3478fc567738803d9c22eeb64a296e21)
> track it. When API or wire-format facts change in code, update this doc in
> the same commit and mirror to Notion. When new narrative (a decision, a
> comparison) lands in either doc, port it to the other before the next
> review cycle.

## Overview

A reusable workflow streams module for Temporal workflows. The workflow holds
an append-only log of `(topic, data)` entries. External clients (activities,
starters, other services) publish and subscribe through the workflow handle
using Temporal primitives (signals, updates, queries).

The module ships as `temporalio.contrib.workflow_stream` in the Python SDK and is
designed to be cross-language compatible. Payloads are opaque byte strings —
the workflow does not interpret them.

## Architecture

```
              ┌──────────────────────────────────────────────────┐
              │                Temporal Workflow                 │
              │                 (WorkflowStream)                 │
              │                                                  │
              │  ┌────────────────────────────────────────────┐  │
              │  │   Append-only log                          │  │
              │  │   [(topic, data), ...]                     │  │
              │  │   base_offset: int                         │  │
              │  │   publishers: {}                           │  │
              │  └────────────────────────────────────────────┘  │
              │                                                  │
  signal ────►│  __temporal_workflow_stream_publish (with dedup) │
  update ────►│  __temporal_workflow_stream_poll (long-poll)     │◄── subscribe()
  query  ────►│  __temporal_workflow_stream_offset               │
              │                                                  │
              │  publish() ── workflow-side                      │
              └──────────────────────────────────────────────────┘
                                    │
                                    │ continue-as-new
                                    ▼
              ┌──────────────────────────────────────────────────┐
              │  WorkflowStreamState carries:                    │
              │    log, base_offset, publishers                  │
              └──────────────────────────────────────────────────┘
```

## API Surface

### Workflow side — `WorkflowStream`

A helper class instantiated from `@workflow.init`. Its constructor
registers the workflow streams signal, update, and query handlers on the current
workflow via `workflow.set_signal_handler`, `workflow.set_update_handler`,
and `workflow.set_query_handler` — there is no base class to inherit.
This matches how other-language SDKs will express the same pattern
(imperative handler registration from inside the workflow body).

```python
from dataclasses import dataclass
from temporalio import workflow
from temporalio.contrib.workflow_stream import WorkflowStream, WorkflowStreamState

@dataclass
class MyInput:
    stream_state: WorkflowStreamState | None = None

@workflow.defn
class MyWorkflow:
    @workflow.init
    def __init__(self, input: MyInput) -> None:
        self.stream = WorkflowStream(prior_state=input.stream_state)

    @workflow.run
    async def run(self, input: MyInput) -> None:
        self.stream.publish("status", b"started")
        await do_work()
        self.stream.publish("status", b"done")
```

Construct `WorkflowStream(...)` once from `@workflow.init`. Include a
`WorkflowStreamState | None` field on your workflow input and always pass it as
`prior_state`: it is `None` on fresh starts and carries accumulated
state across continue-as-new (see [Continue-as-New](#continue-as-new)).
Workflows that will never continue-as-new may call `WorkflowStream()` with no
argument.

Two construction-time guards keep misuse loud:

- **Caller-frame check.** The constructor inspects
  `sys._getframe(1).f_code.co_name` and raises `RuntimeError` if the
  immediate caller is not a method named `__init__`. Calls from
  `@workflow.run`, helpers, or signal/update/query handlers are
  rejected immediately rather than silently producing a workflow that
  registers handlers on every replay.
- **Single-instance check.** Instantiating `WorkflowStream` twice on the same
  workflow raises `RuntimeError`, detected via
  `workflow.get_signal_handler("__temporal_workflow_stream_publish")`.

**Dynamic registration race.** Because the constructor registers
`__temporal_workflow_stream_publish` *dynamically* in `__init__` rather than via
`@workflow.signal` decorators, a synchronous custom signal/update
handler that reads `WorkflowStream` state can observe pre-publish state when
the inbound publish and the custom call land in the same activation.
The recipe is to make such handlers `async` and `await asyncio.sleep(0)`
once before reading state. The module's own `__temporal_workflow_stream_poll`
update is already safe (it is `async` and `await`s
`workflow.wait_condition`). See the README "Gotcha" section for the
full explanation.

| Method / Handler | Kind | Description |
|---|---|---|
| `WorkflowStream(prior_state=None)` | constructor | Initialize internal state and register handlers on the current workflow. Must be called from `@workflow.init`. |
| `publish(topic, value)` | instance method | Append to the log from workflow code. `value` is converted via the workflow's sync payload converter (no codec). |
| `get_state(publisher_ttl=timedelta(seconds=900))` | instance method | Snapshot for CAN. Prunes dedup entries older than TTL. |
| `drain()` | instance method | Unblock polls and reject new ones for CAN. |
| `continue_as_new(build_args, *, publisher_ttl=timedelta(seconds=900))` | async instance method | Drain, wait for handlers, then `workflow.continue_as_new` with `build_args(post_drain_state)`. |
| `truncate(up_to_offset)` | instance method | Discard log entries before offset. |
| `__temporal_workflow_stream_publish` | signal handler | Receives publications from external clients (with dedup). |
| `__temporal_workflow_stream_poll` | update handler | Long-poll subscription: blocks until new items or drain. |
| `__temporal_workflow_stream_offset` | query handler | Returns the current global offset. |

### Client side — `WorkflowStreamClient`

Used by activities, starters, and any code with a workflow handle.

```python
from temporalio.contrib.workflow_stream import WorkflowStreamClient

# Preferred: factory method (enables CAN following + activity auto-detect)
client = WorkflowStreamClient.create(temporal_client, workflow_id)

# --- Publishing (with batching) ---
# Values go through the client's payload converter per item; the codec
# chain (e.g. encryption, compression) runs once at the envelope level.
async with client:
    client.publish("events", TextDelta(delta="hello"))
    client.publish("events", TextDelta(delta=" world"))
    client.publish("events", TextComplete(), force_flush=True)
    client.publish("raw", my_prebuilt_payload)  # zero-copy fast path

# --- Subscribing ---
# Pass result_type=T to have item.data decoded to T via the same codec
# chain. Without result_type, item.data is the raw Payload and the
# caller dispatches on metadata.
async for item in client.subscribe(["events"], result_type=EventUnion):
    print(item.topic, item.data)
    if is_done(item):
        break
```

| Method | Description |
|---|---|
| `WorkflowStreamClient.create(client, wf_id)` | Factory with explicit Temporal client and workflow id. Follows CAN in `subscribe()`. |
| `WorkflowStreamClient.from_activity()` | Factory that pulls client and workflow id from the current activity context. Follows CAN in `subscribe()`. |
| `WorkflowStreamClient(handle)` | From handle directly (no CAN following; no codec chain — falls back to the default converter). |
| `publish(topic, value, force_flush=False)` | Buffer a message. `value` may be any converter-compatible object or a pre-built `Payload`. `force_flush` triggers immediate flush (fire-and-forget). |
| `flush()` | Async. Block until items buffered at call time are confirmed by the server. No-op if nothing is buffered. |
| `subscribe(topics=None, from_offset=0, *, result_type=None, poll_cooldown=timedelta(milliseconds=100))` | Async iterator. `topics` accepts a single string, a list of strings, or `None` (= all topics). `result_type` decodes `item.data` to the given type; omit for raw `Payload`. Always follows CAN chains when created via `create` or `from_activity`. |
| `get_offset()` | Query current global offset. |

The client offers three complementary ways to flush:

1. **Context manager exit** — drains and flushes on `__aexit__`. Best
   when the publisher's lifetime maps cleanly to a scope.
2. **`force_flush=True` on `publish()`** — declarative, fire-and-forget.
   Best when the *event being published* is itself the signal to flush
   (e.g. a "stream complete" sentinel).
3. **`await client.flush()`** — explicit synchronization point that
   returns once buffered items have been acknowledged by the server.
   Best when the caller needs proof that prior publications landed but
   the moment does not correspond to any particular event — e.g.
   "before returning from this activity, make sure everything I have
   published is durable."

#### Activity convenience

Inside an activity, use `WorkflowStreamClient.from_activity()` — the Temporal
client and target workflow id come from the activity context, so the
caller doesn't have to thread them through:

```python
@activity.defn
async def stream_events() -> None:
    client = WorkflowStreamClient.from_activity(batch_interval=timedelta(seconds=2))
    async with client:
        for chunk in generate_chunks():
            client.publish("events", chunk)
            activity.heartbeat()
```

`from_activity()` is a separate factory rather than an overload of
`create()` because silently inferring arguments outside an activity
masks a configuration bug as a runtime error in an unrelated code
path.

## Data Types

```python
from temporalio.api.common.v1 import Payload

@dataclass
class WorkflowStreamItem:
    topic: str
    data: Any          # Payload by default; decoded value when
                       # subscribe is called with result_type=T
    offset: int = 0    # Populated at poll time

@dataclass
class PublishEntry:
    topic: str
    data: str          # Wire: base64(Payload.SerializeToString())

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
    items: list[_WorkflowStreamWireItem]     # Wire-format items
    next_offset: int = 0       # Offset for next poll
    more_ready: bool = False   # Truncated response; poll again

@dataclass
class PublisherState:
    sequence: int
    last_seen: datetime

@dataclass
class WorkflowStreamState:
    log: list[_WorkflowStreamWireItem] = field(default_factory=list)
    base_offset: int = 0
    publishers: dict[str, PublisherState] = field(default_factory=dict)
```

The containing workflow input must type the field as `WorkflowStreamState | None`,
not `Any` — `Any`-typed fields deserialize as plain dicts, losing the type.

### Wire format for payloads

The user-facing `data` on `WorkflowStreamItem` is a
`temporalio.api.common.v1.Payload`, which carries both the data bytes
and the encoding metadata written by the client's data converter and
codec chain. Subscribers can either decode by passing `result_type=T`
to `subscribe()` (runs the async converter chain, including the codec)
or inspect `Payload.metadata` directly for heterogeneous topics.

On the wire, every `data` string is
`base64(Payload.SerializeToString())`. This is because the default
JSON payload converter can serialize a top-level `Payload` as a
signal argument but **cannot** serialize a `Payload` embedded inside
a dataclass (it raises `TypeError: Object of type Payload is not JSON
serializable`). Embedding the proto-serialized bytes keeps the wire
format JSON-compatible while preserving the full `Payload` — metadata
and all — across the signal and update round-trips. Round-trip is
guarded by
`tests/contrib/workflow_stream/test_payload_roundtrip.py`.

## Design Decisions

### 1. Durable streams

All stream events flow through the workflow's append-only log, backed by
Temporal's persistence layer. There is no ephemeral streaming option.

**Trade-off.** Ephemeral streams that skip the Temporal server, or transit it
with lower durability, would be less resource-intensive. We chose durable
streams because:

1. **Simpler programming model.** One event path, one source of truth. The
   application does not need merge logic, reconnection handling for a second
   channel, or fallback behavior when the ephemeral path fails.
2. **Reliability.** Events survive worker crashes, workflow restarts, and
   continue-as-new. A subscriber that connects after a failure sees the
   complete history, not a gap where the ephemeral channel lost events.
3. **Correctness.** With a single path, subscriber code is the same whether
   processing events live or replaying them after a reconnect. A separate
   ephemeral path for latency-sensitive events (e.g., token deltas) would
   create a second code path through the frontend — additional complexity
   that is difficult to test.

The cost is latency: events round-trip through the Temporal server before
reaching the subscriber. Batching (see [Batching is built into the
client](#7-batching-is-built-into-the-client)) manages this — a 0.1-second
interval for token streaming keeps latency acceptable while amortizing
per-signal overhead.

Durability is Temporal's core value proposition. Making the stream durable by
default aligns with the platform.

### 2. Topics are plain strings, no hierarchy

Topics are exact-match strings. No prefix matching, no wildcards. A subscriber
provides a list of topic strings to filter on; an empty list means "all topics."

### 3. Items are Temporal `Payload`s, not opaque bytes

The workflow stores each item as a
`temporalio.api.common.v1.Payload` — the same type signals, updates,
and activities use. Publishers pass any value the client's data
converter accepts (or a pre-built `Payload` for zero-copy);
subscribers either receive the raw `Payload` (for heterogeneous
topics) or pass `result_type=T` to have it decoded.

This replaces an earlier "opaque byte strings" design. We switched
because the opaque-bytes path **skipped the user's codec chain** —
encryption and compression codecs saw only the
outer `PublishInput` envelope, not the individual items. For users
who expect their codec chain to cover every piece of data flowing
through Temporal, that is a silent compliance/correctness gap.

The three original arguments for opaque bytes don't hold up:

1. **Decoupling from the data converter.** Signals and updates
   accept `Any` without making handlers generic; `Payload.metadata`
   carries per-value encoding info. Workflow Streams can do the same.
2. **Layering — transport vs. application.** Every other Temporal
   API surface (signals, updates, activity args, workflow args)
   uses `Payload`. Workflow Streams was the outlier.
3. **Type hints at decode time.** Subscribers pass `result_type` at
   the subscribe boundary — the same pattern as
   `execute_update(result_type=...)`.

**Codec runs once, at the envelope level.** Both
`WorkflowStreamClient.publish` and `WorkflowStream.publish` turn values into
`Payload` via the **sync** payload converter. The codec chain is
not applied per item. It runs once — on the `__temporal_workflow_stream_publish`
signal envelope (client → workflow path) and on the
`__temporal_workflow_stream_poll` update envelope (workflow → subscriber path) —
because Temporal's SDK already runs `DataConverter.encode` on
signal and update args. Running the codec per item *as well*
would double-encrypt / double-compress, and compressing
already-encrypted data is pointless. The per-item `Payload` still
carries the encoding metadata (`encoding: json/plain`,
`messageType`, etc.), so `subscribe(result_type=T)` works
without needing the codec to have run per item.

**Wire format.** `PublishEntry.data` and `_WorkflowStreamWireItem.data` are
base64-encoded `Payload.SerializeToString()` bytes, not nested
`Payload` protos, because the default JSON converter cannot
serialize a `Payload` embedded inside a dataclass. See [Data
Types — Wire format for payloads](#wire-format-for-payloads).

### 4. Global offsets, NATS JetStream model

> 🚪 **One-way door.** Once subscribers persist and resume from global integer
> offsets — stored in SSE `Last-Event-ID`, BFF reconnection state, and
> client-side cursor logic — the offset semantics are baked into the wire
> protocol. Switching to per-topic offsets later would break every existing
> subscriber's resume path. This is the right choice (cursor portability and
> cross-topic ordering are valuable), but recognize that every consumer built
> against this API will assume a single integer is a complete stream position.

Every entry gets a global offset from a single counter. Subscribers filter by
topic but advance through the global offset space.

We surveyed offset models across Kafka, Redis Streams, NATS JetStream, PubNub,
Google Workflow Streams, RabbitMQ Streams, and Amazon SQS/SNS. No major system provides
a true global offset across independent topics. The two closest:

- **NATS JetStream**: one stream captures multiple subjects via wildcards, with
  a single sequence counter. This is our model.
- **PubNub**: wall-clock nanosecond timestamp as cursor across channels.

We evaluated six alternatives for handling the information leakage that global
offsets create (a single-topic subscriber can infer other-topic activity from
gaps): per-topic counts, opaque cursors, encrypted cursors, per-topic lists,
per-topic offsets with cursor hints, and accepting the leakage.

| Option | Systems | Leakage | Cross-topic ordering | Resume cost | Cursor portability |
|---|---|---|---|---|---|
| Per-topic count as cursor | *(theoretical)* | None | Preserved | O(n) or extra state | Coupled to filter |
| Opaque cursor wrapping global offset | *(theoretical)* | Observable | Preserved | O(1) | Filter-independent |
| Encrypted global offset | *(theoretical)* | None | Preserved | O(1) | Filter-independent |
| Per-topic / per-partition lists | Kafka, Redis Streams, RabbitMQ Streams, Google Workflow Streams, SQS/SNS | None | **Lost** | O(1) | N/A |
| **Global offsets (chosen)** | NATS JetStream, PubNub (timestamp variant) | Contained at BFF | Preserved | O(new items) | Filter-independent |
| Per-topic offsets with cursor hints | *(theoretical)* | None | Preserved | O(new items) | Per-topic only |

**Decision:** Global offsets are the right choice for workflow-scoped workflow streams.

**Why not per-topic offsets?** The most sophisticated alternative — per-topic
offsets with opaque cursors carrying global position hints — was rejected
for three reasons:

1. **The threat model doesn't apply.** Information leakage assumes untrusted
   multi-tenant subscribers who shouldn't learn about each other's traffic
   volumes. That's Kafka's world — separate consumers for separate services.
   In workflow-scoped workflow streams, the subscriber is the BFF: trusted server-side
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

### 5. No topic creation

Topics are implicit. Publishing to a topic creates it. Subscribing to a
nonexistent topic returns no items and waits for new ones.

### 6. `force_flush` forces a flush, does not reorder

`force_flush=True` causes the client to immediately flush its buffer. It
does NOT reorder items — the flushed item appears in its natural
position after any previously-buffered items. The purpose is
latency-sensitive delivery, not importance ranking.

### 7. Session ordering

Publications from a single client are ordered. This relies on two Temporal
guarantees:

> "Signals are delivered in the order they are received by the Cluster and
> written to History."
> ([docs](https://docs.temporal.io/workflows#signal))

Specifically: (1) signals sent sequentially from the same client appear in
workflow history in send order, and (2) signal handlers are invoked in
history order. The guarantee breaks down only for *concurrent* signals — if
two signal RPCs are in flight simultaneously, their order in history is
nondeterministic. The `WorkflowStreamClient` flush lock (`_flush_lock`) ensures
signals are never in flight concurrently from a single client:

1. Acquire lock
2. `await handle.signal(...)` — blocks until server writes to history
3. Release lock

Combined with the workflow's single-threaded signal processing (the
`__temporal_workflow_stream_publish` handler is synchronous — no `await`), items within and
across batches from a single publisher preserve their publish order.

Concurrent publishers get a total order in the log (the workflow serializes
all signal processing), but the interleaving is nondeterministic — it depends
on arrival order at the server. Per-publisher ordering is preserved.

Once items are in the log, their order is stable — reads are repeatable.

### 8. Batching is built into the client

`WorkflowStreamClient` includes a Nagle-like batcher (buffer + timer). The async
context manager starts a background flush task; exiting cancels it and does a
final flush. Batching amortizes Temporal signal overhead.

Parameters:
- `batch_interval` (`timedelta`, default 2 seconds): timer between automatic flushes.
- `max_batch_size` (optional): auto-flush when buffer reaches this size.

### 9. Subscription is poll-based, exposed as async iterator

The fundamental primitive is an offset-based long-poll: the subscriber sends
`from_offset` and gets back items plus `next_offset`. `__temporal_workflow_stream_poll` is a
Temporal update with `wait_condition`. `subscribe()` wraps this in an
`AsyncIterator` with a configurable `poll_cooldown` (`timedelta`, default
100ms) to rate-limit polls.

**Trade-off.** The alternative is server-push — the workflow streams system executes
a callback on the subscriber. Pull is better aligned with durable streams:

1. **Back-pressure is natural.** A slow subscriber just polls less
   frequently. Push requires the server to implement flow control to avoid
   overwhelming subscribers — or risk dropping messages, defeating the
   durable-stream purpose.
2. **The subscriber controls its own read position.** It can replay from an
   earlier offset, skip ahead, or resume from exactly where it left off.
   Push requires the server to track per-subscriber delivery state.
3. **Durable streams are data at rest.** The log exists regardless of
   whether anyone is reading it. Pull treats the log as something to read
   from; push treats it as a pipe to deliver through, which fights the
   durability model.

Temporal's architecture reinforces this — there is no server-push mechanism
for external clients. Updates with `wait_condition` are the closest
approximation: the workflow blocks until data is available, making it
behave like push from the subscriber's perspective while remaining pull on
the wire.

**Both layers are exposed.** The offset-based poll is a first-class part
of the API, not hidden behind the iterator. The BFF uses offsets directly
to map SSE event IDs to global offsets for reconnection. Application code
that just wants to process items in order uses the iterator. Different
consumers use different layers.

**Poll efficiency.** The poll slices `self._log[from_offset - base_offset:]`
and filters by topic. The common case — single topic, continuing from last
poll — is O(new items since last poll). The global offset points directly to
the resume position with no scanning or cursor alignment. Multi-topic polls
are the same cost: one slice, one filter pass. The worst case is a poll from
offset 0 (full log scan), which only happens on first connection or after the
subscriber falls behind.

**Fan-out is per-poll, not shared.** Each `__temporal_workflow_stream_poll` update is an
independent Temporal update RPC. The handler has no registry of active
subscribers; every call executes `_on_poll` from scratch with its own
`from_offset` closure and topic set. When a publish grows the log,
Temporal's `wait_condition` machinery re-evaluates every pending predicate
and wakes each one whose condition is now true. Each then slices the same
shared log independently, applies its own topic filter, and returns its own
`PollResult` on its own update response.

The consequences:

- Two subscribers on the same topics from the same offset both receive the
  items — each item travels the wire **twice**, once per update response.
- Two subscribers from different offsets each see their own slice; the
  overlapping range is serialized into both responses.
- Two subscribers with disjoint topics each see a filtered subset; no items
  are duplicated across their responses, but the log is walked twice.

This is deliberate. Temporal updates are 1:1 RPCs, not a shared delivery
fabric. There is no intra-workflow subscriber registry, no cross-poll
dedup, no broadcast. Fan-out cost scales linearly with subscriber count,
but there's no shared state between polls to get wrong and no delivery-order
ambiguity between them. Applications that need to multiplex a single
subscription across many local consumers should do so on the client side,
below the `subscribe()` iterator — one poll stream feeding N in-process
consumers. A workflow-side shared fan-out is listed under
[Future Work](#future-work).

### 10. Workflow can publish but should not subscribe

Workflow code can call `self.publish()` directly — this is deterministic.
Reading from the log within workflow code is possible but breaks the
failure-free abstraction because external publishers send data via signals
(non-deterministic inputs), and branching on signal content creates
replay-sensitive code paths.

### 11. `base_offset` for truncation

The log carries a `base_offset`. All offset arithmetic uses
`offset - base_offset` to index into the log, so discarding a prefix of
consumed entries and advancing `base_offset` keeps global offsets
monotonic. If a poll's `from_offset` is below `base_offset`, the
subscriber has fallen behind truncation and the poll fails with a
non-retryable `TruncatedOffset` error.

Because the module targets continue-as-new as the standard pattern for
long-running workflows, workflow history size is not the binding
constraint — CAN rolls history forward indefinitely. The binding
constraint is the in-memory log growing between CAN boundaries. Voice
streaming workflows have shown this matters in practice: a session can
accumulate tens of thousands of small audio/text events long before CAN
is triggered, and the workflow needs a way to release entries the
subscriber has already consumed without waiting for a CAN cycle.
`WorkflowStream.truncate(up_to_offset)` exposes this — workflow-side only.
Out-of-range offsets raise `ApplicationError` (type
`TruncateOutOfRange`); applications that want external truncation
control must wire up their own signal/update on top of `truncate()`.

When a subscriber whose `from_offset` is below `base_offset` polls,
`WorkflowStreamClient.subscribe()` catches the `TruncatedOffset`
`ApplicationError`, resets `offset` to `0` (which the stream treats
as "from whatever is available"), and continues. The application sees
a gap in offsets but the iterator does not raise.

### 12. No timeout on long-poll

`wait_condition` in the poll handler has no timeout. The poll blocks
indefinitely until one of three things happens:

1. **New data arrives** — the `len(log) > offset` condition fires.
2. **Draining for continue-as-new** — `WorkflowStream.drain()` sets the flag.
3. **Client disconnects** — the BFF drops the SSE connection, cancels the
   update RPC, and the handler becomes an inert coroutine cleaned up at
   the next drain cycle.

A previous design used a 5-minute timeout as a defensive "don't block
forever" mechanism. This was removed because:

- **It adds unnecessary history events.** Every poll creates a `TimerStarted`
  event. For a streaming session doing hundreds of polls, this doubles the
  history event count and accelerates approach to the ~50K event CAN threshold.
- **The drain mechanism already handles cleanup.** `WorkflowStream.drain()` unblocks
  all waiting polls, and the update validator rejects new polls, so
  `all_handlers_finished()` converges without timers.
- **Zombie polls are harmless.** If a client crashes without cancelling, its
  poll handler is just an in-memory coroutine waiting on a condition. It
  consumes no Temporal actions and is cleaned up at the next CAN cycle.

### 13. Signals for publish, updates for poll

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
retry path, and `WorkflowStream` handler logic are unchanged.

## Design Principles

### Deduplication follows the end-to-end principle

**The end-to-end principle** (Saltzer, Reed, Clark, "End-to-End Arguments in
System Design," 1984): a function can be correctly and completely
implemented only with the knowledge available at the endpoints of a
communication system. Implementing it at intermediate layers may be
redundant or of little value, because the endpoints must handle it
regardless. The corollary: implement a function at the lowest layer that
can implement it *completely*. Don't partially implement it at an
intermediate layer.

> 🚪 **One-way door.** The contract that the stream is an append-only log of
> *all* attempts — including failed ones — is irreversible once subscribers
> build reducers around it. Every frontend reducer expects to see interleaved
> retries and uses application-level events (e.g., `AGENT_START` resetting the
> text accumulator) to reconcile. If the transport later started filtering
> retries, existing reducers would break — they would miss the state
> transitions they depend on, and there would be two different behaviors
> depending on whether the subscriber was connected live (saw the failed
> attempt) or replayed after reconnect (didn't). This is the correct design,
> but it is a permanent commitment.

**Our design decision.** We do not filter out events from failed activity
attempts. When an activity retries — for example, an LLM call that times
out, or a tool call that fails because a worker crashes — its previous
attempt's streaming events remain in the log. The new attempt publishes
fresh events. The subscriber sees both.

**Why the workflow streams layer cannot handle this completely.** When an LLM
activity retries, the model runs again and produces different output —
different tokens, different wording, a different response. The workflow streams
layer sees two different message sequences. It has no way to know these
represent the same logical operation. Only the application knows that the
second response supersedes the first.

We could have added retry semantics to the workflow streams protocol — for example,
tagging messages with attempt numbers and letting the transport filter
superseded attempts, similar to signal-level dedup. But this would be
incomplete, and the incompleteness creates a real problem: if the
transport scrubs failed-attempt events, but the subscriber already saw
them in real time (before the retry happened), the subscriber now has two
code paths — one for the live stream (which included the failed attempt)
and one for replay after reconnect (which doesn't). Two paths through the
frontend for the same logical scenario is a source of bugs and is
difficult to test. The transport's filtering doesn't save the subscriber
any work; the subscriber needs robust reconciliation logic regardless.

**The contract: an append-only log of attempts.** The stream records what
happened, including failed attempts. The subscriber decides how to present
this to the user. In our frontend, the application-layer reducer handles
reconciliation: a new `TEXT_COMPLETE` event overwrites the previous one
(set semantics), and an `AGENT_START` event resets the text accumulator so
the retry's tokens replace the failed attempt's partial output. This
reducer produces the same state whether it processes events live or
replays them on reconnect — there is only one code path.

**The workflow streams layer handles what it can handle completely.** Signal-level
dedup (same publisher ID + same sequence number) is fully resolvable at the
transport layer — the layer has all the information it needs, so it
deduplicates there. Activity-level dedup cannot be fully resolved at the
transport layer — it requires application context — so the workflow streams layer
does not attempt it. Each layer handles the duplicates it can completely
resolve.

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

Each `WorkflowStreamClient` instance generates a 16-hex-char identifier (`publisher_id`, the prefix of a fresh UUID4) on creation.
Each `flush()` increments a monotonic `sequence` counter. The signal payload
includes both. The workflow tracks the highest seen sequence per publisher in
`_publishers: dict[str, PublisherState]` and rejects any signal with
`sequence <= existing.sequence`.

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
                "__temporal_workflow_stream_publish",
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

`publishers` is `dict[str, PublisherState]` — bounded by number of publishers
(typically 1-2), not number of flushes. Carried through continue-as-new in
`WorkflowStreamState`. If `publisher_id` is empty (workflow-internal publish),
dedup is skipped.

Each `PublisherState` records the highest accepted `sequence` and the
`workflow.now()` at which it was accepted (`last_seen: datetime`). During
`WorkflowStream.get_state(publisher_ttl=timedelta(seconds=900))`, entries older
than TTL are pruned to bound memory across long-lived workflow chains.

**Safety constraint**: `publisher_ttl` must exceed the client's
`max_retry_duration`. If a publisher's dedup entry is pruned while it still
has a pending retry, the retry could be accepted as new, creating duplicates.

### Scope: what workflow streams dedup does and does not handle

Duplicates arise at three points in the pipeline. Each layer handles the
duplicates it introduces — applying the end-to-end principle (Saltzer, Reed,
Clark 1984).

```
LLM API  -->  Activity  -->  WorkflowStreamClient  -->  Workflow Log  -->  BFF/SSE  -->  Browser
  (A)                            (B)                                (C)
```

| Type | Cause | Handled by |
|---|---|---|
| A: Duplicate LLM work | Activity retry produces a second, semantically equivalent but textually different response | Application layer (activity idempotency keys, workflow orchestration) |
| B: Duplicate signal batches | Signal retry after ambiguous failure delivers the same `(publisher_id, sequence)` batch twice | **Workflow Streams layer** (`sequence <= existing.sequence` rejection) |
| C: Duplicate SSE events | Browser reconnects and BFF replays previously-delivered events | Delivery layer (SSE `Last-Event-ID`, idempotent frontend reducers) |

**Why Type A doesn't belong here.** Data escapes to the subscriber during the
first LLM call — tokens are consumed, forwarded to the browser, and rendered
before any retry occurs. By the time a retry produces a duplicate response,
the original is already consumed. The workflow streams layer has no opportunity to
suppress it, and resolution requires application semantics (discard, replace,
merge) that the transport layer has no knowledge of.

**Why Type B must be here.** The consumer sees `WorkflowStreamItem(topic, data)` with
no unique ID. If the workflow accepted a duplicate batch, the duplicates would
get fresh offsets and be indistinguishable from originals. Content-based dedup
has false positives (an LLM legitimately produces the same token twice; a
status event like `{"type":"THINKING_START"}` repeats across turns). The
`(publisher_id, sequence)` check is the only correct implementation — it
preserves transport encapsulation and uses context only the transport layer
has.

**Why Type C doesn't belong here.** SSE reconnection is below the workflow streams
layer. The BFF assigns gapless event IDs and maps `Last-Event-ID` back to
global offsets (see [Information Leakage and the BFF](#information-leakage-and-the-bff)).

## Continue-as-New

### Problem

`WorkflowStream` accumulates workflow history through signals (each
`__temporal_workflow_stream_publish`) and updates (each `__temporal_workflow_stream_poll` response). Over a
streaming session, history grows toward the ~50K event threshold. CAN resets
the history while carrying the canonical log copy forward.

### State

```python
@dataclass
class PublisherState:
    sequence: int
    last_seen: datetime

@dataclass
class WorkflowStreamState:
    log: list[_WorkflowStreamWireItem] = field(default_factory=list)
    base_offset: int = 0
    publishers: dict[str, PublisherState] = field(default_factory=dict)
```

The `log` carries the wire form (`_WorkflowStreamWireItem`, base64-encoded
`Payload.SerializeToString()`) so the snapshot serializes through
the default JSON converter without needing to embed proto `Payload`s
in a dataclass. The constructor decodes them back to `WorkflowStreamItem`
on the new run.

`WorkflowStream(prior_state=...)` restores all three fields. `WorkflowStream.get_state()`
snapshots them.

### Draining

A long-poll `__temporal_workflow_stream_poll` blocks indefinitely until new data arrives. To
allow CAN to proceed, draining uses two mechanisms:

1. **`WorkflowStream.drain()`** sets a flag that unblocks all waiting poll handlers
   (the `or self._draining` clause in `wait_condition`).
2. **Update validator** rejects new polls when draining, so no new handlers
   start and `all_handlers_finished()` stabilizes.

`WorkflowStream.continue_as_new(build_args)` packages the three steps:

```python
# CAN sequence in the parent workflow:
await self.stream.continue_as_new(lambda state: [WorkflowInput(
    stream_state=state,
)])
```

`build_args` runs *after* drain stabilizes, with the post-drain
`WorkflowStreamState` as its single argument. Workflows that need to override
other CAN parameters (`task_queue`, `retry_policy`, `run_timeout`, etc.)
fall back to the explicit recipe:

```python
self.stream.drain()
await workflow.wait_condition(workflow.all_handlers_finished)
workflow.continue_as_new(args=[WorkflowInput(
    stream_state=self.stream.get_state(),
)], task_queue="other-tq")
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
- Post-CAN: `WorkflowStream(prior_state=...)` restores N items. New appends start
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
`WorkflowStreamClient` across concurrent `subscribe()` calls is safe.

## Information Leakage and the BFF

Global offsets leak cross-topic activity (a single-topic subscriber sees gaps).
This is acceptable within the workflow streams API because the subscriber is the BFF —
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

start_offset = await stream.get_offset()
async for item in stream.subscribe(topics=["events"], from_offset=start_offset):
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

Any Temporal client in any language can interact with a workflow streams workflow by:

1. **Publishing**: Signal `__temporal_workflow_stream_publish` with `PublishInput` payload
2. **Subscribing**: Execute update `__temporal_workflow_stream_poll` with `PollInput`, loop
3. **Checking offset**: Query `__temporal_workflow_stream_offset`

Double-underscore prefix on handler names avoids collisions with application
signals/updates. The envelope types are simple composites of strings, bytes,
and ints — representable in every Temporal SDK's default data converter.

**Requires the default (JSON) data converter on the envelope.** The wire
protocol depends on all participants — workflow, publishers, and
subscribers — using the default JSON data converter for the
*envelope* types (`PublishInput`, `PollInput`, `PollResult`,
`WorkflowStreamState`). These are simple composites of strings, ints, and
lists that serialize identically across SDKs under the default
converter. A custom converter that changes how dataclasses serialize
(protobuf-based, etc.) would break cross-language interop on the
envelope.

The user-facing data inside each envelope is a Temporal `Payload`,
serialized to bytes and base64-encoded as a string field on
`PublishEntry` / `_WorkflowStreamWireItem`. The user's payload converter and codec
chain run once at the envelope level and apply to the whole batch
(see [Design Decision 3](#3-items-are-temporal-payloads-not-opaque-bytes)).
Cross-language clients interoperate by using the same shape:
JSON-encoded envelopes containing base64-of-serialized-`Payload` items.

## Compatibility

> 🚪 **One-way door (two parts).**
>
> **Immutable handler names.** `__temporal_workflow_stream_publish`, `__temporal_workflow_stream_poll`, and
> `__temporal_workflow_stream_offset` are permanent wire-level entry points. The escape hatch —
> versioned handler names like `__temporal_workflow_stream_v2_poll` — gets more expensive over
> time: `WorkflowStream` must register all supported versions, with no discovery
> mechanism for which versions a workflow supports.
>
> **No version field.** Committing to additive-only evolution means the *only*
> path for a true breaking change is versioned handler names. If the
> additive-only discipline ever fails — an existing field's semantics need to
> change, not just a new field added — there is no graceful migration path
> within a single handler. The argument against a version field is sound
> (signals are fire-and-forget, so version rejection equals silent data loss),
> but it means the protocol's evolvability hinges entirely on never needing to
> change existing field semantics.

The wire protocol evolves under four rules to prevent accidental breakage by
future contributors.

### Alternatives considered

We evaluated and rejected five approaches to protocol evolution in favor of
additive-only.

**Version field in payloads.** Add `version: int` to each wire type and have
the receiver check it. Fatal flaw: signals are fire-and-forget. If a v1
workflow receives a v2 signal and rejects it based on version, the publisher
never learns the signal was rejected — silent data loss. Strictly worse than
the current behavior, where unknown fields are harmlessly dropped by
Temporal's JSON deserializer. For updates (poll), a version mismatch could
return an error, but this only helps if you change the semantics of an
existing field — which you should not do (that is a new handler, not a
version bump).

**Versioned handler names** (e.g., `__temporal_workflow_stream_v2_poll`). The most robust
option — creates entirely separate protocol surfaces so old and new code
never interact. But premature: `WorkflowStream` would have to register handlers for
all supported versions, the client must probe which versions exist (Temporal
has no "does this handler exist?" primitive), and dead code accumulates.
Reserved as the escape hatch for a future true breaking change.

**Protocol negotiation.** Client declares version in poll, workflow
responds with what it supports. Turns `WorkflowStream` into a version-dispatching
router. Disproportionate complexity. Temporal's Worker Versioning (Build ID
routing) solves this better at the infrastructure level — route tasks to
compatible workers rather than negotiating at the message level.

**SDK version embedding.** Couples the protocol to the SDK release cadence.
SDK version 2.0 might change zero protocol fields; SDK version 1.7 might
change three. The version number becomes meaningless noise.

**Accepting silent incompatibility.** Letting version drift just break
silently. Unacceptable for a durable-stream contract: a v2 subscriber
hitting a v1 workflow should see older fields default, not corrupt state.

**Why additive-only works.** Every protocol change to date has followed
the same pattern: new field with a default that preserves pre-feature
behavior. This matches Protocol Buffers wire compatibility rules (never
change the meaning of an existing field number; always provide defaults
for new fields) and Avro's schema evolution model. Temporal's own
mechanisms cover the hard cases:

- **Worker Versioning (Build IDs):** For true breaking changes, deploy v2
  `WorkflowStream` on a new Build ID. Old workflows continue on old workers; new
  workflows start on new workers. Strictly more powerful than
  message-level versioning because it operates at the workflow execution
  level.
- **`workflow.patched()`:** For in-workflow behavior branching during
  replay. Gates old vs. new logic within the same workflow code during
  transition periods.

**Ecosystem parallel.** Kafka's inter-broker protocol uses explicit version
numbers because brokers in a cluster must negotiate capabilities at
connection time — a fundamentally different topology from our
single-workflow-instance model. Our pattern is closer to protobuf wire
evolution: the schema is the contract, defaults handle absence, and
breaking changes get a new message type (handler name).

### 1. Additive-only wire evolution

New fields on `PublishInput`, `PollInput`, `PollResult`, and `WorkflowStreamState` must
have defaults. Existing field semantics must not change. Temporal's JSON data
converter drops unknown fields on deserialization and uses defaults for missing
fields, so additive changes are safe in both directions (new client → old
workflow, and vice versa). This is the same model as Protocol Buffers wire
compatibility.

### 2. Handler names are immutable

`__temporal_workflow_stream_publish`, `__temporal_workflow_stream_poll`, and `__temporal_workflow_stream_offset` will never change
meaning. If a future change is incompatible with additive evolution, the correct
mechanism is a new handler name (e.g., `__temporal_workflow_stream_v2_poll`) — creating an
entirely separate protocol surface so old and new code never interact.

### 3. `WorkflowStreamState` must be forward-compatible

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
  change, deploy the new `WorkflowStream` on a new Build ID. Old running workflows
  continue on old workers; new workflows start on new workers. This operates at
  the infrastructure level — handling in-flight workflows, replay, and
  mixed-version fleets — which message-level version fields cannot.
- **`workflow.patched()` handles in-workflow transitions.** If a new `WorkflowStream`
  version changes behavior (e.g., how it processes a signal), `patched()` gates
  old vs. new logic within the same workflow code during the transition period.

### Field defaults

All fields follow rule 1:

| Field | Default | Behavior when absent |
|---|---|---|
| `PublishInput.publisher_id` | `""` | Empty string skips dedup |
| `PublishInput.sequence` | `0` | Zero skips dedup |
| `_WorkflowStreamWireItem.offset` | `0` | Zero means "unknown" |
| `PollResult.more_ready` | `False` | No truncation signaled |
| `WorkflowStreamState.publishers` | `{}` | No dedup or TTL state |

## Ecosystem analogs

The closest analogs in established messaging systems, for orientation:

- **Offset model** — NATS JetStream: one stream, multiple subjects, a
  single monotonic sequence number. Subscribers filter by subject but
  advance through the global sequence space. This is our model.
- **Idempotent producer** — Kafka's producer ID + monotonic sequence
  number, scoped to the broker. Our `publisher_id` + `sequence` at the
  workflow does the same job, scoped to signal delivery into one workflow.
- **Blocking pull** — Redis Streams `XREAD BLOCK`. Our `__temporal_workflow_stream_poll`
  update with `wait_condition` is the Temporal-native equivalent.
- **Durable-execution peer** — the Workflow SDK ([workflow-sdk.dev](https://workflow-sdk.dev))
  has a first-class streaming model with indexed resumption and buffered
  writes, but uses external storage (Redis/filesystem) as the broker
  rather than the workflow itself.

Full comparison tables (same/different with Kafka, NATS JetStream, Redis
Streams, and Workflow SDK) live on the
[Streaming API Design Considerations Notion page](https://www.notion.so/3478fc567738803d9c22eeb64a296e21).

## Future Work

### Shared workflow-side fan-out

Each `__temporal_workflow_stream_poll` update today is serviced independently, and an item
published to N interested subscribers crosses the wire N times (see
[Design Decision 9](#9-subscription-is-poll-based-exposed-as-async-iterator)).
For low fan-out (1–2 consumers) this is fine; for workloads with many
concurrent subscribers on overlapping topics the duplication becomes the
dominant cost.

A shared fan-out would keep a registry of active polls inside the
workflow, coalesce them by `(from_offset, topics)` key, and have one
poll wake-up build a shared response that the handler returns to every
matching caller. The tricky parts are: (a) offsets and topic filters
usually differ per subscriber, limiting coalescing; (b) the registry is
workflow state that must survive continue-as-new; (c) cancelled polls
must be reaped cleanly so the registry doesn't leak across replays.
Until a concrete workload shows the linear-in-subscribers cost matters,
the simpler per-poll model is the right default — applications that need
local fan-out can share one `subscribe()` iterator across N in-process
consumers on the client side, where state is trivial.

### Workflow-defined filters and transforms

Today the only filter is "topic in topics". A richer model would let
the workflow register named filters or transforms — e.g., `filter="high_priority"`
or `transform="redact_pii"` — that run inside the poll handler before
items are returned. This keeps computation close to the log, avoids
shipping items the subscriber will discard, and lets workflows enforce
access control per subscriber rather than delegating it to clients.

Design questions left open: filter/transform registration API (at
`WorkflowStream` construction, or later?), whether transforms may change the
item count (e.g., aggregation), how filter state interacts with
continue-as-new, and how filter identity is named on the wire for
cross-language clients.

### Replace workflow-side dedup with server-side `request_id`

Workflow-side `(publisher_id, sequence)` dedup
([Exactly-Once Publish Delivery](#exactly-once-publish-delivery))
exists because Temporal's built-in signal `request_id` dedup does not
cover the cases the contrib needs:

1. **Within a single `_flush()` call**, sdk-core's retry layer reuses the
   same `request_id` across attempts, so the server already dedups
   transient RPC failures. We get this for free.
2. **Across `_flush()` calls** (the `_pending` retry loop), each call
   to `await handle.signal(...)` allocates a fresh `request_id` —
   `temporalio/client.py:8357` hardcodes `request_id=str(uuid.uuid4())`,
   with no way to override. The server cannot recognize that two such
   calls are the same logical batch, so the workflow-side check is
   what guarantees exactly-once.
3. **Across continue-as-new**, even if (1) and (2) were perfect,
   `pendingSignalRequestedIDs` is per-run mutable state and is not
   carried by `addWorkflowExecutionStartedEventForContinueAsNew`. A
   retry whose first attempt landed on run N and whose retry lands on
   run N+1 is accepted as fresh. Verified empirically on the Temporal
   dev server and Temporal Cloud (see
   `experiments/can-signal-dup/README.md` in the repo root for the
   reproduction). [temporalio/temporal#4021](https://github.com/temporalio/temporal/issues/4021)
   tracks the related state-growth concern that has historically
   discouraged extending the dedup set across CAN.

If both (a) the SDK exposes `request_id` on
`WorkflowHandle.signal()` and (b) the server dedups by `request_id`
across continue-as-new, the workflow-side check becomes redundant and
can be removed. The migration is mechanical because the dedup keys at
both layers are already aligned.

**What changes:**

```python
# In _client.py, _flush() — pin a deterministic request_id:
await self._handle.signal(
    "__temporal_workflow_stream_publish",
    PublishInput(
        items=batch,
        publisher_id=self._publisher_id,
        sequence=seq,
    ),
    request_id=f"{self._publisher_id}:{seq}",   # NEW
)
```

```python
# In _stream.py, __temporal_workflow_stream_publish handler — drop the dedup branch:
def _on_publish(self, input: PublishInput) -> None:
    # remove: if input.publisher_id and input.sequence ...
    for entry in input.items:
        self._log.append(
            WorkflowStreamItem(topic=entry.topic, data=_decode_payload(entry.data))
        )
```

```python
# In WorkflowStreamState — this field becomes unused and can be removed in a
# follow-up wire migration (see Compatibility):
# publishers: dict[str, PublisherState]
```

**What stays:**

- The client-side `_pending` retry loop and `_flush_lock`. Server-side
  `request_id` dedup makes retries safe; it does not eliminate the
  reasons we retry (long outages, worker restarts).
- The `(publisher_id, sequence)` shape on the wire. We continue to
  send them — they are the inputs we'd derive `request_id` from, and
  keeping them on the wire preserves observability and lets older
  workflow versions that still maintain the dedup table interoperate
  with newer clients during rollout.
- `force_flush=True`, `flush()`, `__aexit__` flush — orthogonal.

**What goes away:**

- `publishers` in `WorkflowStreamState`.
- `publisher_ttl` and the `publisher_ttl > max_retry_duration` safety
  constraint — there is no longer a per-publisher map to expire.

**Migration path:**

1. Land the SDK change to expose `request_id` on signals.
2. Confirm server `request_id` dedup spans CAN (re-run
   `experiments/can-signal-dup` against a server build that includes
   the fix).
3. Bump the contrib protocol minor version. Newer clients send the
   pinned `request_id`; older clients still send fresh UUIDs. Both
   continue to set `(publisher_id, sequence)` so a workflow that has
   not yet been re-deployed remains correct.
4. After all clients are upgraded, deploy a workflow version that
   ignores `(publisher_id, sequence)` and relies on the server. Drop
   the dedup fields from `WorkflowStreamState` in a subsequent wire-format
   pass once the old fields are no longer read by any deployed
   version.

Until both prerequisites are real, the workflow-side dedup is
load-bearing and must stay.

### Workflow-side subscription

[Design Decision 10](#10-workflow-can-publish-but-should-not-subscribe)
explains why workflow code shouldn't read the log directly today — the
log contains data from non-deterministic signal inputs, and branching on
it creates replay-sensitive code paths. There are workflow-side use
cases (aggregator workflows, workflows that fan events out to child
workflows, workflows that trigger activities based on stream content)
where a proper subscription API would be useful.

A safe workflow-side `subscribe()` would need to tag reads so they go
through the same determinism machinery as other non-deterministic
inputs — likely surfaced as an async iterator that yields at
deterministic checkpoints. The simplest cut is probably a pull-based
iterator over `self._log` slices that integrates with `wait_condition`
for the "no data yet" case, mirroring the external poll API but
bypassing the update RPC layer.

### Why `continue_as_new` takes a state-bound builder

The helper's signature is
`continue_as_new(build_args: Callable[[WorkflowStreamState], Sequence[Any]])`.
Two earlier shapes were rejected:

1. **Eager-args form** —
   `continue_as_new(args=[WorkflowInput(stream_state=self.get_state(), ...)])`.
   Python evaluates call-site arguments before the method body runs,
   so `self.get_state()` would snapshot state *before* `drain()` and
   `all_handlers_finished` — the opposite of the recipe's intent.
2. **Zero-arg builder** — `build_args: Callable[[], Sequence[Any]]`,
   the lambda inspecting `self.stream` directly. Defers evaluation
   correctly, but leaves the caller free to write
   `self.stream.get_state()` inside the lambda, where the
   "evaluated post-drain" contract is implicit and only documented in
   prose.

Passing the post-drain `WorkflowStreamState` as the lambda's parameter makes
the contract structural: there is one path to the state and the helper
controls when it's read. The signature itself reads as "here is the
state, return the CAN args."

The helper deliberately does *not* mirror the full
`workflow.continue_as_new` signature (12 parameters today). Workflows
that need to override `task_queue` / `retry_policy` / `run_timeout` /
etc. fall back to the explicit `drain` / `wait_condition` /
`workflow.continue_as_new(...)` recipe — keeping the helper's surface
area stable as new CAN options land in `temporalio.workflow`.

## File Layout

```
temporalio/contrib/workflow_stream/
├── __init__.py                  # Public API exports
├── _stream.py                   # WorkflowStream (workflow-side)
├── _client.py                   # WorkflowStreamClient (external-side)
├── _types.py                    # Shared data types
├── README.md                    # Usage documentation
└── DESIGN.md                    # This document
```
