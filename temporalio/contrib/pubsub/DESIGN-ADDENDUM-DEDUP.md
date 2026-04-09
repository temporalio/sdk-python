# Exactly-Once Publish Delivery — Addendum

Addendum to [DESIGN.md](./DESIGN.md). Addresses the signal delivery gap: the
original design has no deduplication, so a retry after a failed signal can
produce duplicate entries in the log.

## Problem

The `PubSubClient.flush()` method sends buffered items to the workflow via a
Temporal signal. If the signal call raises an exception (e.g., network timeout
on the response after the server accepted the signal), the client cannot
distinguish "signal was delivered" from "signal was not delivered." Without
deduplication, the client must choose:

- **Clear buffer before sending (swap pattern).** Items are lost if the signal
  truly fails. At-most-once.
- **Clear buffer after sending.** Items are re-sent on the next flush if the
  signal was delivered but the response failed. At-least-once with silent
  duplication.

Neither is acceptable for a pub/sub log where subscribers expect exactly-once
delivery and stable offsets.

## Options Considered

### Option 1: Batch UUID

Each flush assigns a `uuid4` to the batch. The workflow maintains a set of seen
batch IDs and skips duplicates.

- **Pro:** Simple to implement.
- **Con:** The seen-IDs set grows without bound. Must be carried through
  continue-as-new or periodically pruned. Pruning requires knowing which IDs
  can never be retried — which is unknowable without additional protocol.

### Option 2: Offset-based dedup

The publisher includes the expected log offset in the signal. The workflow
rejects if items at that offset already exist.

- **Pro:** No additional state — dedup is implicit in the log structure.
- **Con:** The publisher does not know the current log offset. It would need to
  query first, introducing a read-before-write round-trip and a race between
  the query and the signal. Multiple concurrent publishers would conflict.

### Option 3: Publisher ID + sequence number

Each `PubSubClient` generates a UUID on creation (the publisher ID). Each flush
increments a monotonic sequence counter. The signal payload includes
`(publisher_id, sequence)`. The workflow tracks the highest seen sequence per
publisher and rejects any signal with a sequence ≤ the recorded value.

- **Pro:** Dedup state is `dict[str, int]` — bounded by the number of
  publishers (typically 1–2), not the number of flushes. The workflow can
  detect gaps (missing sequence numbers) as a diagnostic signal. Naturally
  survives continue-as-new if carried in state. No unbounded set. No
  read-before-write round-trip.
- **Con:** Requires the publisher to maintain a sequence counter (trivial) and
  the workflow to carry `publisher_sequences` through CAN (small dict).

### Option 4: Temporal idempotency keys

Temporal does not currently provide built-in signal deduplication or idempotency
keys for signals. This option is not available.

## Design Decision: Publisher ID + sequence number (Option 3)

Option 3 is adopted. The dedup state is minimal, bounded, and self-cleaning
(old publishers' entries can be removed after a timeout or on CAN). It aligns
with how Kafka producers achieve exactly-once: each producer has an ID and a
monotonic sequence, and the broker deduplicates on the pair.

## Wire Changes

### `PublishInput`

```python
@dataclass
class PublishInput:
    items: list[PublishEntry] = field(default_factory=list)
    publisher_id: str = ""
    sequence: int = 0
```

Both fields default to empty/zero for backward compatibility. If `publisher_id`
is empty, the workflow skips deduplication (legacy behavior).

### `PubSubClient` changes

```python
class PubSubClient:
    def __init__(self, handle, ...):
        ...
        self._publisher_id: str = uuid.uuid4().hex
        self._sequence: int = 0

    async def flush(self) -> None:
        async with self._flush_lock:
            if self._buffer:
                self._sequence += 1
                batch = self._buffer
                self._buffer = []
                try:
                    await self._handle.signal(
                        "__pubsub_publish",
                        PublishInput(
                            items=batch,
                            publisher_id=self._publisher_id,
                            sequence=self._sequence,
                        ),
                    )
                except Exception:
                    # Restore items for retry. Sequence number is already
                    # incremented — the next attempt uses the same sequence,
                    # so the workflow deduplicates if the first signal was
                    # actually delivered.
                    self._sequence -= 1
                    self._buffer = batch + self._buffer
                    raise
```

Key behaviors:

- **Buffer swap before send.** Items are moved out of the buffer before the
  signal await. New `publish()` calls during the await write to the fresh
  buffer and are not affected by a retry.
- **Sequence advances on failure.** If the signal raises, the sequence counter
  is NOT decremented. The failed batch is restored to the buffer, but the next
  flush uses a new sequence number. This prevents data loss: if the original
  signal was delivered but the client saw an error, items published during the
  failed await would be merged into the retry batch. With the old sequence,
  the workflow would deduplicate the entire merged batch, silently dropping
  the newly-published items. With a new sequence, the retry is treated as a
  fresh batch. The tradeoff is that the original items may be delivered twice
  (at-least-once), but the workflow-side dedup catches the common case where
  the batch is retried unchanged.
- **Lock for coalescing.** An `asyncio.Lock` serializes flushes. Multiple
  concurrent `flush()` callers queue on the lock; by the time each enters,
  later items have accumulated. This naturally coalesces N flush calls into
  fewer signals.

## Workflow Changes

### Signal handler

```python
@workflow.signal(name="__pubsub_publish")
def _pubsub_publish(self, input: PublishInput) -> None:
    self._check_initialized()
    if input.publisher_id:
        last_seq = self._publisher_sequences.get(input.publisher_id, 0)
        if input.sequence <= last_seq:
            return  # duplicate — skip
        self._publisher_sequences[input.publisher_id] = input.sequence
    for entry in input.items:
        self._pubsub_log.append(PubSubItem(topic=entry.topic, data=entry.data))
```

If `publisher_id` is empty (legacy or workflow-internal publish), dedup is
skipped. Otherwise, the workflow compares the incoming sequence against the
highest seen for that publisher. If it's ≤, the entire batch is dropped as a
duplicate.

### Internal state

```python
self._publisher_sequences: dict[str, int] = {}
```

Initialized in `init_pubsub()` from `PubSubState.publisher_sequences`.

## Continue-as-New State

`PubSubState` gains a `publisher_sequences` field:

```python
@dataclass
class PubSubState:
    log: list[PubSubItem] = field(default_factory=list)
    base_offset: int = 0
    publisher_sequences: dict[str, int] = field(default_factory=dict)
```

This is carried through CAN so that dedup survives across runs. The dict is
small — one entry per publisher that has ever sent to this workflow, typically
1–2 entries.

### Cleanup on CAN

Stale publisher entries (from publishers that are no longer active) accumulate
but are harmless — they're just `str: int` pairs. If cleanup is desired, the
workflow can remove entries for publishers that haven't sent in N runs, but this
is not required for correctness.

## Sequence Gap Detection

If the workflow receives sequence N+2 without seeing N+1, it indicates a lost
signal. The current design does **not** act on this — it processes the batch
normally and records the new high-water mark. Gaps are expected to be rare
(they require a signal to be truly lost, not just slow), and the publisher will
retry with the same sequence if it didn't get an ack.

A future extension could log a warning on gap detection for observability.

## Properties

- **Exactly-once delivery.** Each `(publisher_id, sequence)` pair is processed
  at most once. Combined with at-least-once retry on the client, this achieves
  exactly-once.
- **Bounded dedup state.** One `int` per publisher. Does not grow with the
  number of flushes.
- **No read-before-write.** The publisher does not need to query the workflow
  before sending.
- **Backward compatible.** Empty `publisher_id` disables dedup. Existing code
  without the field works as before.
- **CAN-safe.** Publisher sequences survive continue-as-new in `PubSubState`.

## Relationship to Other Addenda

- [Continue-as-new addendum](./DESIGN-ADDENDUM-CAN.md): `PubSubState` shape
  updated with `publisher_sequences`. Drain/validator mechanics unaffected.
- [Topic offsets addendum](./DESIGN-ADDENDUM-TOPICS.md): Unaffected. Dedup
  operates on the publish path; offsets and cursors operate on the subscribe
  path.
