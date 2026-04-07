# Topic Offsets and Cursor Design — Addendum

Addendum to [DESIGN.md](./DESIGN.md). Revises section 3 ("Global monotonic
offsets, not per-topic") after evaluating per-topic offset models. Concludes
that global offsets are the right choice for workflow-scoped pub/sub, with
information leakage addressed at the BFF layer rather than the pub/sub API.

## Problem

The original design assigns every log entry a global monotonic offset regardless
of topic. A single-topic subscriber sees gaps in offset numbers — e.g., offsets
0, 3, 7, 12. These gaps leak information about activity on other topics. A
subscriber to `"events"` can infer the volume of traffic on `"thinking"` or
`"status"` from the size of the gaps, even though it has no direct access to
those topics.

This is an information leakage concern, not a correctness bug.

## Industry Survey

We surveyed offset/cursor models across major pub/sub and streaming systems to
inform the design.

| System | Cursor Scope | Unified Multi-Topic Cursor? |
|---|---|---|
| Kafka | Per-partition offset (int64) | No — separate offset per partition per topic |
| Redis Streams | Per-stream entry ID (timestamp-seq) | No — separate ID per stream |
| NATS JetStream | Per-stream sequence (uint64) | Yes — one stream captures multiple subjects |
| PubNub | Per-channel timetoken (nanosecond timestamp) | Yes — single timestamp spans channels |
| Google Pub/Sub | Per-subscription ack set | No |
| RabbitMQ Streams | Per-stream offset (uint64) | No |
| Amazon SQS/SNS | Ack-and-delete (no offset) | No |

**Key finding:** No major system provides a true global offset across
independent topics. The two that offer unified multi-topic cursors do it
differently:

- **NATS JetStream** defines a single stream that captures messages from
  multiple subjects (via wildcards). The stream has one sequence counter.
  Interleaving happens at write time. This is closest to our design.

- **PubNub** uses a wall-clock nanosecond timestamp as the cursor, so a single
  timetoken naturally spans channels. The tradeoff is timestamp-based ordering
  rather than sequence-based.

Every other system requires the consumer to maintain independent cursors per
topic/partition/stream.

## Options Considered

### Option A: Per-topic item count as cursor

The subscriber's cursor represents "I've seen N items matching my filter." The
workflow translates that back to a global log position internally.

- **Pro:** Zero information leakage. Total ordering preserved internally.
- **Con:** Resume requires translating per-topic offset → global log position.
  Either O(n) scan on every poll, or a per-topic index that adds state to
  manage through continue-as-new. Also, the cursor is coupled to the topic
  filter — a cursor from `subscribe(["events"])` is meaningless if you later
  call `subscribe(["events", "status"])`.

### Option B: Opaque cursor wrapping the global offset

Cursor is typed as `str`, documented as opaque. Internally contains the global
offset.

- **Pro:** Zero internal complexity. O(1) resume. Cursor works regardless of
  topic filter changes.
- **Con:** Information leakage remains observable to anyone who inspects cursor
  values across polls. "Opaque" is a social contract, not a technical one.
  Gaps in the underlying numbers are still visible.

### Option C: Encrypted/HMAC'd global offset

Same as B but cryptographically opaque.

- **Pro:** Leakage is technically unobservable.
- **Con:** Requires a stable key across continue-as-new. Introduces crypto into
  workflow code (determinism concerns). Complexity disproportionate to the
  threat model — the subscriber already has access to its own data.

### Option D: Per-topic offsets everywhere

Separate log per topic. Each topic has its own 0-based sequence.

- **Pro:** No leakage by construction. Simplest mental model per topic.
- **Con:** Loses total cross-topic ordering. Multi-topic subscription requires
  merging N streams with no defined interleaving. More internal state. More
  complex continue-as-new serialization.

### Option E: Accept the leakage

Keep global offsets exposed as-is (original design).

- **Pro:** Simplest implementation. Offset = list index.
- **Con:** The information leakage identified above.

### Option F: Per-topic offsets with cursor hints

Per-topic offsets on the wire, single global log internally, opaque cursors
carrying a global position hint for efficient resume.

- **Pro:** Zero information leakage. Global insertion order preserved. Efficient
  resume via hints. Graceful degradation if hints are stale.
- **Con:** Cursor parsing/formatting logic. `topic_counts` dict that survives
  continue-as-new. Multi-cursor alignment algorithm. Cursors are per-topic,
  not portable across filter changes. Complexity unjustified for expected log
  sizes (thousands of items where a filtered slice is microseconds).

### Summary

| | Leakage | Ordering | Resume cost | Complexity | Cursor portability |
|---|---|---|---|---|---|
| A. Per-topic count | None | Preserved | O(n) or extra state | Medium | Coupled to filter |
| B. Opaque global | Observable | Preserved | O(1) | Minimal | Filter-independent |
| C. Encrypted global | None | Preserved | O(1) | High | Filter-independent |
| D. Per-topic lists | None | **Lost** | O(1) | High | N/A |
| E. Accept it | Yes | Preserved | O(1) | None | Filter-independent |
| F. Per-topic + hints | None | Preserved | O(new items) | Medium-High | Per-topic only |

## Design Decision: Global offsets with BFF-layer containment

We evaluated per-topic offset models (Options A, D, F) and concluded that the
complexity is not justified. The information leakage concern is real but is
better addressed at the trust boundary (the BFF) than in the pub/sub API itself.

### Why not per-topic offsets?

The subscriber in our architecture is the BFF — trusted server-side code that
could just as easily subscribe to all topics. The threat model for information
leakage assumes untrusted multi-tenant subscribers (Kafka's world: separate
consumers for separate services). That does not apply to workflow-scoped
pub/sub, where one workflow serves one subscriber through a server-side proxy.

Per-topic cursors (Option F) also sacrifice cursor portability. A global offset
is a stream position that works regardless of which topics you filter on.
Changing your topic filter does not invalidate your cursor. Per-topic cursors
are coupled to the filter — you need a separate cursor per topic, and adding a
topic to your subscription requires starting that topic from the beginning.

### Why not just accept the leakage (Option E)?

We accept the leakage **within the pub/sub API** (between workflow and BFF) but
contain it there. The global offset must not leak to the end client (browser).
The BFF is the trust boundary: it consumes global offsets from the workflow and
presents a clean, opaque interface to the browser.

### The NATS JetStream model

Our design follows the NATS JetStream model: one stream, multiple subjects, one
sequence counter. The industry survey identified this as the closest analogue,
and we adopt it directly. Topics are labels for server-side filtering, not
independent streams with independent cursors.

### Information leakage containment at the BFF

The BFF assigns its own gapless sequence numbers to SSE events using the
standard SSE `id` field. The browser sees `id: 1`, `id: 2`, `id: 3` — no gaps,
no global offsets, no information about other topics.

On reconnect, the browser sends `Last-Event-ID` (built into the SSE spec). The
BFF maps that back to a global offset internally and resumes the subscription.

This keeps:
- The **workflow API** simple (global offsets, single integer cursor)
- The **browser API** clean (SSE event IDs, no workflow internals)
- The **mapping** where it belongs (the BFF, which is the trust boundary)

### Final design

**Global offsets internally and on the pub/sub wire. Single append-only log.
BFF contains the leakage by assigning SSE event IDs at the trust boundary.**

### Wire types

```python
@dataclass
class PubSubItem:
    topic: str
    data: bytes

@dataclass
class PollInput:
    topics: list[str] = field(default_factory=list)
    from_offset: int = 0
    timeout: float = 300.0

@dataclass
class PollResult:
    items: list[PubSubItem]
    next_offset: int = 0
```

`PubSubItem` does not carry an offset. The global offset is an internal detail
exposed only through `PollResult.next_offset` and the `get_offset()` query.

### `get_offset()` remains public

The `__pubsub_offset` query returns the current log length (next offset). This
is essential for the "snapshot the watermark, then subscribe from there" pattern
used by the BFF:

```python
start_offset = await pubsub.get_offset()  # capture position before starting work
# ... start the agent turn ...
async for item in pubsub.subscribe(topics=["events"], from_offset=start_offset):
    yield sse_event(item)
```

### Internal state

```python
self._pubsub_log: list[PubSubItem]   # single ordered log, all topics
self._base_offset: int = 0           # global offset of log[0]
```

The `base_offset` is 0 today. It exists to support future log truncation: when
a prefix of the log is discarded (e.g., after continue-as-new compaction), the
base offset advances so that global offsets remain monotonic across the
workflow's lifetime. All log access uses `self._pubsub_log[offset - self._base_offset]`.
If `offset < self._base_offset`, the subscriber has fallen behind the
truncation point — this is an error.

Log truncation and compaction are deferred to a future design iteration. Until
then, the log grows without bound and `base_offset` remains 0.

### Poll algorithm

Given `from_offset = 4702`:

1. Compute log index: `start = from_offset - self._base_offset`.
2. If `start < 0`, the subscriber fell behind truncation — raise error.
3. Slice: `self._pubsub_log[start:]`.
4. Filter to requested topics (if any).
5. Return filtered items plus `next_offset = self._base_offset + len(self._pubsub_log)`.

**Efficiency:** O(new items since last poll). The global offset points directly
to where the last poll left off. No scanning, no alignment, no cursor parsing.

### Continue-as-new state

```python
class PubSubState(BaseModel):
    log: list[PubSubItem]
    base_offset: int = 0
```

The full log is carried through continue-as-new. Truncation (discarding a
prefix and advancing `base_offset`) is deferred to a future iteration.

### Properties

- **No leakage to end clients.** Global offsets stay between workflow and BFF.
  The browser sees SSE event IDs assigned by the BFF.
- **Global insertion order preserved.** Poll responses return items in the order
  they were published, across all requested topics.
- **Efficient resume.** O(new items) — the offset points directly to the
  resume position.
- **Cursor portability.** The global offset works regardless of topic filter.
  Change your topic filter without invalidating your cursor.
- **Simple internal state.** One list, one integer. No auxiliary data structures,
  no per-topic indices, no cursor parsing.
- **Truncation-ready.** `base_offset` supports future log prefix removal
  without changing the offset model or the external API.

## Relationship to Other Addenda

The [continue-as-new addendum](./DESIGN-ADDENDUM-CAN.md) remains valid. The
CAN state shape is `PubSubState` with `log` and `base_offset`. The
drain/validator/follow-CAN-chain mechanisms are unaffected.
