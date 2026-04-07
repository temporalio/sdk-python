# Analysis: End-to-End Principle Applied to Deduplication

Should pub/sub dedup live in the workflow (middle layer), or should
consumers handle it at the edges? This analysis applies the end-to-end
argument to the different types of duplicates in the system.

## The End-to-End Argument

Saltzer, Reed, and Clark (1984): a function can be correctly and
completely implemented only with the knowledge and help of the
application standing at the endpoints. Putting it in the middle layer
may improve performance but cannot guarantee correctness — the endpoints
must still handle the failure cases themselves.

Applied here: if the consumer must handle duplicates anyway (because some
duplicates originate above or below the transport layer), then dedup in
the pub/sub workflow is redundant complexity.

## The Pipeline

```
LLM API  -->  Activity  -->  PubSubClient  -->  Workflow Log  -->  BFF/SSE  -->  Browser
  (1)           (2)              (3)              (4)              (5)           (6)
```

Duplicates can arise at stages 1, 3, and 5. Each has different
characteristics.

## Types of Duplicates

### Type A: Duplicate LLM Responses (Stage 1)

**Cause**: Activity retries. If an activity calling an LLM times out but
the LLM actually completed, the retry produces a second, semantically
equivalent but textually different response.

**Nature**: The two responses have *different content*. They are not
byte-identical duplicates — they are duplicate *requests* that produce
duplicate *work*.

**Why this doesn't belong in pub/sub**: Not because pub/sub can't detect
it — in principle, you could fingerprint content or track LLM request
IDs in the workflow. The real reason is that **data escapes to the
application before you know whether dedup will be needed.** The activity
streams the first LLM response through the pub/sub log as tokens arrive.
The subscriber consumes them. The BFF forwards them to the browser. The
user sees them rendered. All of this happens during the first LLM call,
before any retry occurs.

By the time the activity fails and retries, the first response's tokens
are already consumed, rendered, and acted upon. The duplicate LLM
response hasn't been produced yet — it doesn't exist until the retry
completes. So there is no point during the first call where the pub/sub
layer could suppress it, because at that point there is nothing to
suppress.

When the retry does produce a second response, the application must
decide what to do: discard it, replace the first, merge them, show both.
That decision depends on application semantics that the pub/sub layer
has no knowledge of. The correct place for this dedup is the activity
(don't retry completed LLM calls), the orchestrating workflow (use
activity idempotency keys), or the application's own recovery logic.

**End-to-end verdict**: Type A dedup belongs at the application layer,
not because pub/sub lacks the capability, but because the data has
already escaped before the duplicate exists.

### Type B: Duplicate Signal Batches (Stage 3)

**Cause**: `PubSubClient._flush()` sends a signal. The server accepts it
but the client sees a network error. The client retries, sending the
same batch again. The workflow receives both signals.

**Nature**: Byte-identical duplicate batches with the same
`(publisher_id, sequence)`.

**Why this belongs in pub/sub**: Two reasons.

First, **encapsulation**: the fact that publishing goes through batched
signals is an implementation detail of the pub/sub transport. The
consumer shouldn't need to know about `(publisher_id, sequence)`, batch
boundaries, or signal retry semantics. Leaking batch-level dedup to the
consumer would couple it to the transport mechanism. If we later switch
to updates, change the batching strategy, or introduce a different
transport, the consumer's dedup logic would break.

Second, **the consumer cannot do it correctly**. The subscriber sees
`PubSubItem(topic, data)` — items have no unique ID. If the workflow
accepts a duplicate batch, it assigns *new* offsets to the duplicate
items, making them indistinguishable from originals. Content-based dedup
has false positives (an LLM legitimately produces the same token twice;
a status event like `{"type":"THINKING_START"}` is repeated across
turns). The consumer would need to implement a fragile, heuristic dedup
that still misses edge cases.

The pub/sub layer, by contrast, can detect these duplicates cheaply and
precisely: `sequence <= last_seen` is a single integer comparison per
batch. The sequence number is generated and validated within the same
control boundary (publisher client + workflow handler). This is not a
"middle layer redundantly implementing endpoint functionality" — it is
the only layer with sufficient context to do it correctly.

**End-to-end verdict**: Type B dedup is properly placed in the workflow.
It preserves transport encapsulation and is the only correct
implementation.

### Type C: Duplicate SSE Delivery (Stage 5)

**Cause**: Browser reconnection. The SSE connection drops, the browser
reconnects with `Last-Event-ID`, and the BFF replays from that offset.
If the BFF replays too far back, the browser sees duplicate events.

**Nature**: Exact replay of previously-delivered events.

**Where dedup must live**: The **BFF** (stage 5) and/or the **browser**
(stage 6). The BFF must track SSE event IDs and resume from the correct
point. The browser/frontend reducer should be idempotent — applying the
same event twice should not corrupt state (e.g., append a text delta
twice).

**End-to-end verdict**: Pub/sub dedup is irrelevant for Type C. This
duplicate exists below the pub/sub layer, in the SSE transport.

## Summary Table

| Type | Cause | Why not in pub/sub? | Where dedup belongs |
|---|---|---|---|
| A: Duplicate LLM work | Activity retry | Data escapes before duplicate exists | Activity / workflow orchestration |
| B: Duplicate batches | Signal retry | *Does* belong in pub/sub | Workflow (pub/sub layer) |
| C: Duplicate SSE events | Browser reconnect | Below the pub/sub layer | BFF / browser |

## Proper Layering

Each layer handles the duplicates it introduces:

```
┌─────────────────────────────────────────────────────────┐
│  Application layer (activity / workflow orchestration)   │
│  Handles: Type A — duplicate LLM work                   │
│  Mechanism: activity idempotency keys, don't retry      │
│  completed LLM calls, application recovery logic        │
├─────────────────────────────────────────────────────────┤
│  Transport layer (pub/sub workflow)                      │
│  Handles: Type B — duplicate signal batches              │
│  Mechanism: (publisher_id, sequence) dedup               │
│  Encapsulates: batching, signals, retry semantics        │
├─────────────────────────────────────────────────────────┤
│  Delivery layer (BFF / SSE / browser)                    │
│  Handles: Type C — duplicate SSE events                  │
│  Mechanism: Last-Event-ID, idempotent reducers           │
└─────────────────────────────────────────────────────────┘
```

Each layer is self-contained. The application doesn't know about signal
batches. The pub/sub layer doesn't know about LLM semantics. The SSE
layer doesn't know about either. Duplicates are resolved at the layer
that introduces them, with the context needed to resolve them correctly.

## Does the Consumer Need Type B Dedup Anyway?

The end-to-end argument would apply if consumers needed Type B dedup
regardless of what the workflow does. They don't:

1. **Consumers cannot detect Type B duplicates.** Items have no unique
   ID. Offsets are assigned by the workflow — if it accepts a duplicate
   batch, the duplicates get fresh offsets and are indistinguishable.

2. **Consumers already handle Type C independently.** SSE reconnection
   and idempotent reducers are standard patterns that exist regardless
   of what the pub/sub layer does.

3. **Type A is handled above.** The activity/workflow prevents duplicate
   work from being published in the first place.

The consumer does *not* need Type B dedup. The layers are clean.

## Conclusion

The `(publisher_id, sequence)` dedup protocol is correctly placed in the
pub/sub workflow. It handles the one type of duplicate that originates
within the transport layer, using context that only the transport layer
has, without leaking transport implementation details to the consumer.

What the pub/sub layer should *not* attempt:
- Type A dedup (duplicate LLM work) — data has already escaped to the
  application before the duplicate exists; resolution requires
  application semantics
- Type C dedup (SSE reconnection) — below the pub/sub layer
- General-purpose content dedup — false positive risk, wrong abstraction
  level
