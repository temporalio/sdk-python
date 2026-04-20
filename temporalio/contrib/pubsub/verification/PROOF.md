# Proof of Exactly-Once Delivery

Formal verification that the pub/sub dedup protocol guarantees no duplicates
and no data loss, for any number of published items.

## Protocol

A client flushes batches of items to a workflow via Temporal signals:

1. **Buffer swap**: `pending = buffer; buffer = []`
2. **Assign sequence**: `pending_seq = confirmed_seq + 1`
3. **Send signal** with `(publisher_id, pending_seq, pending)`
4. **On success**: `confirmed_seq = pending_seq; pending = None`
5. **On failure**: keep `pending` and `pending_seq` for retry

The workflow deduplicates: reject if `sequence <= last_seen_seq[publisher_id]`.

The network is non-deterministic: a signal may be delivered to the workflow
but the client may see a failure (e.g., network timeout on the response).

## Properties

- **NoDuplicates** (safety): each item appears at most once in the workflow log.
- **OrderPreserved** (safety): items appear in the log in the order they were
  published. This is stronger than within-batch ordering — it covers
  cross-batch ordering too.
- **AllItemsDelivered** (liveness): under fairness, every published item
  eventually reaches the log. Note: the TLA+ spec models a protocol without
  `max_retry_duration`. The implementation intentionally sacrifices this
  liveness property by dropping pending batches after a timeout to bound
  resource usage. This is a design choice — when a batch is dropped, items
  may be lost if the signal was not delivered.

## Bounded Model Checking

`PubSubDedup.tla` models the protocol with TLC model checking:

| MaxItems | States Generated | Distinct States | Depth | Result |
|----------|-----------------|-----------------|-------|--------|
| 4 | 320 | 175 | 19 | Pass |
| 6 | 1,202 | 609 | 27 | Pass |

NoDuplicates, OrderPreserved (invariants) and AllItemsDelivered (liveness
under weak fairness) all pass.

## Inductive Invariant (Unbounded Argument)

Bounded model checking proves correctness for specific MaxItems values.
To extend to all N, we define a strengthened invariant `IndInv` in
`PubSubDedupInductive.tla` and verify that it holds for all reachable
states under the standard specification.

Note: TLC checks `IndInv` as a reachable-state invariant of `Spec`
(i.e., `Init => IndInv` and preservation along all reachable behaviors),
not as a true inductive invariant from arbitrary `IndInv` states.
The per-action proof sketch below argues inductiveness informally.
Since the invariant's clauses are structural relationships independent
of N, verification at MaxItems=6 gives high confidence in the general
case.

### Definition

`IndInv` has 13 clauses organized into 5 groups:

**Uniqueness (C1-C3):** Items are unique within each container.
- C1: `Unique(wf_log)` — no duplicates in the log
- C2: `Unique(buffer)` — no duplicates in the buffer
- C3: `Unique(pending)` — no duplicates in the pending batch

**Disjointness (C4-C5):** Buffer items are always fresh.
- C4: `Disjoint(buffer, pending)`
- C5: `Disjoint(buffer, wf_log)`

**Dedup relationship (C6-C7):** The critical property linking pending to the log.
- C6: If `pending_seq > wf_last_seq` (not yet delivered), then `Disjoint(pending, wf_log)`
- C7: If `pending_seq <= wf_last_seq` (already delivered), then `IsSubseq(pending, wf_log)`

**Sequence consistency (C8-C11):** Sequence numbers track delivery correctly.
- C8: `confirmed_seq <= wf_last_seq`
- C9: `pending = <<>> => confirmed_seq = wf_last_seq`
- C10: `pending = <<>> <=> pending_seq = 0`
- C11: `pending /= <<>> => pending_seq = confirmed_seq + 1`

**Bounds (C12-C13):** All item IDs are in `1..item_counter`.

### IndInv implies NoDuplicates

Trivially: NoDuplicates is clause C1.

### Init implies IndInv

All containers are empty, all counters are 0. Every clause is vacuously true
or directly satisfied.

### IndInv is preserved by every action

**Publish:** Adds `item_counter + 1` to buffer. This ID is fresh — not in
any container (by C12, all existing IDs are in `1..item_counter`). Uniqueness
and disjointness are preserved. `item_counter` increments, so C12 holds for
the new ID.

**StartFlush (retry):** No changes to buffer, pending, or wf_log. Only
`flushing` and `delivered` change. All structural properties preserved.

**StartFlush (new):** Requires `pending = <<>>`. By C9, `confirmed_seq = wf_last_seq`.
So `pending_seq' = confirmed_seq + 1 = wf_last_seq + 1 > wf_last_seq`.
Buffer moves to pending: C2 (buffer unique) transfers to C3 (pending unique).
C5 (buffer disjoint from log) transfers to C6 (pending disjoint from log,
since `pending_seq' > wf_last_seq`). New buffer is `<<>>`, satisfying C4-C5
vacuously.

**Deliver (accepted, `pending_seq > wf_last_seq`):** Appends pending to wf_log.
By C6, pending is disjoint from wf_log. Combined with C1 (log unique) and
C3 (pending unique), the extended log has no duplicates → C1 preserved.
Sets `wf_last_seq' = pending_seq`, so now `pending_seq <= wf_last_seq'`.
Pending items are in the new log → C7 satisfied. C5 preserved: buffer was
disjoint from both pending and old log, so disjoint from new log.

**Deliver (rejected, `pending_seq <= wf_last_seq`):** wf_log unchanged.
Sets `delivered = TRUE`. All properties trivially preserved.

**FlushSuccess:** Requires `delivered = TRUE` (so Deliver has fired). Sets
`confirmed_seq' = pending_seq`, `pending' = <<>>`. By C11,
`pending_seq = confirmed_seq + 1`. The Deliver action that set
`delivered = TRUE` either accepted (setting `wf_last_seq = pending_seq`)
or rejected (leaving `wf_last_seq` unchanged, which means
`pending_seq <= wf_last_seq` was already true — but since
`pending_seq = confirmed_seq + 1` and `confirmed_seq <= wf_last_seq` (C8),
we need `wf_last_seq >= confirmed_seq + 1 = pending_seq`). In both cases,
`wf_last_seq >= pending_seq` after Deliver. FlushSuccess requires
`delivered = TRUE`, meaning Deliver fired. If Deliver accepted,
`wf_last_seq = pending_seq`. If Deliver rejected, `pending_seq <= wf_last_seq`
was already true. So `confirmed_seq' = pending_seq <= wf_last_seq`, and
since `confirmed_seq <= wf_last_seq` is C8 (not strict equality), C8 is
preserved. C9 requires `pending = <<>> => confirmed_seq = wf_last_seq`.
After FlushSuccess, `pending' = <<>>` and `confirmed_seq' = pending_seq`.
If Deliver accepted: `wf_last_seq = pending_seq = confirmed_seq'` → C9 holds.
If Deliver rejected: `pending_seq <= wf_last_seq`, so `confirmed_seq' <= wf_last_seq`.
But can `confirmed_seq' < wf_last_seq`? Only if another delivery advanced
`wf_last_seq` past `pending_seq` — but there is only one publisher, so no.
In the single-publisher model, `wf_last_seq` is only set by Deliver for
this publisher's `pending_seq`, so after acceptance `wf_last_seq = pending_seq`.
If rejected, `wf_last_seq` was already `>= pending_seq`, but since only
this publisher writes to `wf_last_seq`, and the last accepted sequence was
`confirmed_seq` (by C9 before StartFlush), and `pending_seq = confirmed_seq + 1`,
we have `wf_last_seq >= confirmed_seq + 1 = pending_seq`. If Deliver rejected,
it means `wf_last_seq >= pending_seq` already, but the only way `wf_last_seq`
could exceed `confirmed_seq` is from a previous delivered-but-not-confirmed
flush — which is exactly `pending_seq`. So `wf_last_seq = pending_seq`,
and C9 holds. Clearing pending makes C3, C4, C6, C7 vacuously true.

**FlushFail:** Sets `flushing' = FALSE`. No changes to buffer, pending,
wf_log, or sequences. All properties preserved.

### Why this generalizes beyond MaxItems

The 13 clauses of IndInv are structural relationships between containers
(uniqueness, disjointness, subset, sequence ordering). None depends on the
value of MaxItems or the total number of items published. The per-action
preservation arguments above use only these structural properties, not any
bound on N.

TLC verifies IndInv for all 609 reachable states at MaxItems=6. The
proof sketch above argues inductiveness informally — since the clauses
are structural relationships independent of N, this gives high
confidence in the general case.

## Order Preservation

`OrderPreserved` states that items appear in the log in ascending order of
their IDs. This is verified as an invariant alongside NoDuplicates.

The property follows from the protocol structure:

1. `Publish` assigns monotonically increasing IDs (`item_counter + 1`)
2. `StartFlush` moves the entire buffer to pending, preserving order
3. `Deliver` appends the entire pending sequence to the log, preserving order
4. Retries re-send the same pending with the same order; dedup ensures only
   one copy appears in the log
5. The flush lock serializes batches, so all items in batch N have lower IDs
   than all items in batch N+1

For multi-publisher scenarios (`PubSubDedupTTL.tla`), ordering is preserved
**per publisher** but not globally across publishers, since concurrent
publishers interleave non-deterministically. The `OrderPreservedPerPublisher`
invariant verifies this.

## TTL-Based Pruning of Dedup Entries

### Problem

`publisher_sequences` grows with each distinct publisher. During
continue-as-new, stale entries (from publishers that are no longer active)
waste space. TTL-based pruning removes entries that haven't been updated
within a time window.

### Safety Constraint

`PubSubDedupTTL.tla` models two publishers with a `Prune` action that
resets a publisher's `wf_last` to 0 (forgetting its dedup history).

**Unsafe pruning** (prune any publisher at any time) violates NoDuplicates.
TLC finds the counterexample in 9 states:

```
1. Publisher A sends batch [1,3] with seq=1
2. Delivered to workflow (log=[1,3], wf_last[A]=1)
3. Client sees failure, keeps pending for retry
4. Retry starts (same pending, same seq=1)
5. PruneUnsafe: wf_last[A] reset to 0 (TTL expired!)
6. Deliver: seq=1 > 0 → accepted → log=[1,3,1,3] — DUPLICATE
```

The root cause: the publisher still has an in-flight retry, but the workflow
has forgotten its dedup entry.

**Safe pruning** (prune only when the publisher has no pending batch and is
not flushing) preserves NoDuplicates. TLC verifies this across 7,635 states
with 2 publishers and MaxItemsPerPub=2.

### Implementation Constraint

The TLA+ safety condition `pend[p] = <<>> /\ ~flush_active[p]` translates
to a real-world constraint: **TTL must exceed the maximum time a publisher
might retry a failed flush.** In practice:

- `PubSubClient` instances are ephemeral (activity-scoped or request-scoped)
- When the activity completes, the client is gone — no more retries
- A 15-minute TTL exceeds any reasonable activity execution time
- During CAN, `get_pubsub_state()` prunes entries older than TTL
- The workflow should wait for activities to complete before triggering CAN

### Multi-Publisher Protocol

The base multi-publisher protocol (without pruning) also passes all
properties: NoDuplicates, OrderPreservedPerPublisher, and AllItemsDelivered.
5,143 states explored with 2 publishers and MaxItemsPerPub=2.

## Retry Timeout (DropPending)

### Problem

The implementation drops pending batches after `max_retry_duration` to bound
resource usage. This sacrifices `AllItemsDelivered` (liveness) for the dropped
batch — an intentional design choice. However, the original implementation
had a bug: it cleared `_pending` without advancing `_sequence` (confirmed_seq).

### Bug: Sequence Reuse After Timeout

`DropPendingBuggy` in `PubSubDedup.tla` models the buggy timeout path.
TLC finds a `SequenceFreshness` violation in 7 states:

```
1. Publish item 1
2. StartFlush: pending=[1], seq=1, buffer=[]
3. Deliver (accepted): wf_log=[1], wf_last_seq=1
4. FlushFail: client sees failure, pending=[1] retained
5. Publish items 2, 3 during retry window
6. DropPendingBuggy: pending cleared, confirmed_seq still 0
7. SequenceFreshness VIOLATED: confirmed_seq=0 < wf_last_seq=1
```

The consequence: the next batch gets `seq = confirmed_seq + 1 = 1`, which
the workflow has already accepted. The batch is silently rejected (dedup),
and items 2, 3 are permanently lost.

### SequenceFreshness Invariant

The key safety property is:

```
SequenceFreshness ==
    (pending = <<>>) => (confirmed_seq >= wf_last_seq)
```

This ensures the next batch's sequence (`confirmed_seq + 1`) is strictly
greater than `wf_last_seq`, preventing silent dedup. It is a weakening of
clause C9 from `IndInv` (which requires strict equality). The weakening is
necessary because `DropPendingFixed` may leave `confirmed_seq > wf_last_seq`
when the dropped signal was never delivered — this is harmless, as the next
batch simply uses a higher-than-necessary sequence number.

### Fix: Advance Sequence Before Clearing Pending

`DropPendingFixed` advances `confirmed_seq` to `pending_seq` before clearing
pending. TLC verifies all invariants (NoDuplicates, OrderPreserved,
SequenceFreshness) across 489 distinct states with MaxItems=4.

| Spec | States | Distinct | SequenceFreshness | NoDuplicates |
|------|--------|----------|-------------------|--------------|
| BuggyDropSpec | 241 | 162 | **FAIL** | Pass |
| FixedDropSpec | 891 | 489 | Pass | Pass |

Note: `NoDuplicates` passes for both — the bug causes data **loss**, not
duplicates. Only a safety invariant about sequence freshness catches it.
The original `AllItemsDelivered` liveness property (as formulated with `<>`)
cannot detect this bug because `<>P` is satisfied at an intermediate state
before the lost items are published.

### Correspondence to Implementation

| TLA+ | Python |
|------|--------|
| `DropPendingFixed` | `_flush()` timeout path: `self._sequence = self._pending_seq` before clearing |

## Scope and Limitations

The TLA+ specs model the core dedup protocol. The following implementation
paths are not modeled beyond what is covered above:

- **`max_retry_duration` timeout**: Modeled as `DropPendingFixed` (see above).
  Dropping a batch sacrifices liveness for that batch only. `NoDuplicates`
  (safety) and `SequenceFreshness` are preserved by the fix.

- **Late delivery after client failure**: The model only allows `Deliver`
  while `flushing = TRUE`. In practice, a signal could be delivered after the
  client observes failure and stops flushing. This cannot cause duplicates:
  if the signal is delivered between FlushFail and the next retry StartFlush,
  `wf_last_seq` advances to `pending_seq`. When the retry fires, Deliver
  sees `pending_seq <= wf_last_seq` and rejects (dedup). If the signal was
  already delivered before FlushFail, the retry is also rejected.

- **Empty `publisher_id` (dedup bypass)**: When `publisher_id` is empty,
  the workflow skips dedup entirely. This path is not modeled — it's
  intentionally at-least-once for workflow-internal publishes.

- **Workflow-internal `publish()`**: Deterministic, no signal involved, no
  dedup needed. Not modeled because there's no concurrency to verify.

- **TTL pruning is assumption-dependent**: `PruneSafe` in the TLA+ spec
  requires `pend[p] = <<>> /\ ~flush_active[p]`. The implementation
  approximates this via timestamps (`publisher_ttl > max_retry_duration`).
  Safety depends on the user aligning these two settings.

- **Publisher ID uniqueness**: The TLA+ model uses fixed publisher identities
  (`{"A", "B"}`). The implementation uses random 64-bit UUIDs
  (`uuid.uuid4().hex[:16]`). If two client instances received the same
  publisher ID and the first's dedup entry was pruned, the second could
  have its sequence 1 accepted even though the first's sequence 1 was
  already delivered. Collision probability is ~2^-64, making this
  practically impossible, but the safety argument implicitly relies on
  publisher ID uniqueness across the TTL window.

## Counterexample: Broken Algorithm

`PubSubDedupBroken.tla` models the old algorithm where on failure the client:
- Restores items to the main buffer
- Advances the sequence number

TLC finds a NoDuplicates violation in 10 states:

```
State 1:  Initial (empty)
State 2:  Publish item 1
State 3:  StartFlush: in_flight=[1], seq=1, buffer=[]
State 4-6: Publish items 2,3,4 (arrive during flush)
State 7:  Deliver: wf_log=[1], wf_last_seq=1 (signal delivered)
State 8:  FlushFail: buffer=[1,2,3,4], confirmed_seq=1 (BUG: item 1 restored)
State 9:  StartFlush: in_flight=[1,2,3,4], seq=2
State 10: Deliver: wf_log=[1,1,2,3,4] — DUPLICATE!
```

The root cause: item 1 was delivered (in the log) but also restored to the
buffer under a new sequence number, bypassing the workflow's dedup check.

The correct algorithm prevents this by keeping the failed batch **separate**
(`pending`) and retrying with the **same** sequence number. If the signal was
already delivered, the retry is deduplicated (same sequence). If it wasn't,
the retry delivers it.

## Correspondence to Implementation

| TLA+ Variable | Python Implementation |
|---|---|
| `buffer` | `PubSubClient._buffer` |
| `pending` | `PubSubClient._pending` |
| `pending_seq` | `PubSubClient._pending_seq` |
| `confirmed_seq` | `PubSubClient._sequence` |
| `wf_last_seq` | `PubSubMixin._pubsub_publisher_sequences[publisher_id]` |

| TLA+ Action | Python Code |
|---|---|
| `Publish` | `PubSubClient.publish()` appends to `_buffer` |
| `StartFlush` (retry) | `_flush()` detects `_pending is not None` |
| `StartFlush` (new) | `_flush()` swaps: `batch = _buffer; _buffer = []` |
| `Deliver` | Temporal signal delivery + `_pubsub_publish` handler |
| `FlushSuccess` | Signal call returns without exception |
| `FlushFail` | Signal call raises; `_pending` retained for retry |
