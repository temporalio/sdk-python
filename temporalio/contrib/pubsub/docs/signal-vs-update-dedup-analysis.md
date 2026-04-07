# Analysis: Signal vs Update for Publishing — Deduplication Tradeoffs

Should pub/sub publishing use signals (current) or updates? This analysis
examines what Temporal provides natively for deduplication and whether
application-level dedup can be eliminated.

## What Temporal Provides

### Signals

- **Delivery guarantee**: at-least-once.
- **Request-level dedup**: the gRPC layer attaches a random `request_id` to
  each RPC. If the SDK's internal retry resends the *same* RPC (e.g., due to
  a transient gRPC error), the server deduplicates it. This is transparent
  and not controllable by the application.
- **No application-level dedup key**: there is no way to attach an
  idempotency key to a signal. If the client makes a *new* signal call with
  the same logical content (a retry after a timeout where the outcome is
  unknown), Temporal treats it as a distinct signal and delivers it.
- **Official guidance**: "For Signals, you should use a custom idempotency
  key that you send as part of your own signal inputs, implementing the
  deduplication in your Workflow code."
  ([docs](https://docs.temporal.io/handling-messages#exactly-once-message-processing))

### Updates

- **Delivery guarantee**: exactly-once *per workflow run*, via Update ID.
- **Update ID**: defaults to a random UUID but can be set by the caller. The
  server deduplicates accepted updates by Update ID within a single workflow
  execution.
- **Cross-CAN boundary**: Update ID dedup state does *not* persist across
  continue-as-new. A retry that lands on a new run is treated as a new
  update.
- **Known bug (temporal/temporal#6375)**: `CompleteUpdate` is sometimes not
  honored when in the same WFT completion as CAN. The frontend retries and
  the update can be delivered to the post-CAN run as a distinct update.
  This makes cross-CAN dedup unreliable even for updates.
- **Official guidance**: "If you are using Updates with Continue-As-New you
  should implement the deduplication in your Workflow code, since Update ID
  deduplication by the server is per Workflow run."

### Summary

| | Signals (current) | Updates |
|---|---|---|
| Per-run dedup | None (app must provide) | Built-in via Update ID |
| Cross-CAN dedup | None (app must provide) | None (app must provide) |
| App-level dedup needed? | **Yes** | **Yes** (for CAN workflows) |

Since pub/sub workflows use continue-as-new, **application-level dedup is
required regardless of whether we use signals or updates for publishing.**

**Pragmatic view**: The cross-CAN update dedup gap (temporal/temporal#6375)
is a known issue that Temporal will likely fix. If we used updates for
publishing and accepted this edge case as a temporary platform limitation,
we could eventually drop application-level dedup entirely once the fix
ships. With signals, application-level dedup is a permanent requirement —
there are no plans to add signal idempotency keys to the platform.

## Tradeoffs Beyond Dedup

### Latency and blocking

| | Signals | Updates |
|---|---|---|
| Client blocks? | No — fire-and-forget | Yes — until workflow processes it |
| Flush latency | ~0 (signal enqueued at server) | Round-trip to worker + processing |
| Caller impact | `publish()` never blocks | Flush blocks for ~10-50ms |

With signals, the flush is non-blocking. The client can immediately continue
buffering new items. With updates, the flush would block until the workflow
worker processes the batch and returns a result.

For high-throughput publishing from activities (e.g., streaming LLM tokens),
the non-blocking property matters. The activity can buffer tokens at whatever
rate they arrive without being throttled by the workflow's processing speed.

### Backpressure

| | Signals | Updates |
|---|---|---|
| Natural backpressure | No | Yes |
| Overflow risk | Workflow history grows unbounded | Client slows to workflow speed |

Updates provide natural backpressure: a fast publisher automatically slows
down because each flush blocks. With signals, a fast publisher can
overwhelm the workflow's event history (each signal adds events). The
current mitigation is batching (amortizes signal count) and relying on the
workflow to CAN before history gets too large.

### Batching

Batching works identically with either approach. The client-side buffer/swap/
flush logic is unchanged — only the flush transport differs:

```python
# Signal (current)
await self._handle.signal("__pubsub_publish", PublishInput(...))

# Update (alternative)
await self._handle.execute_update("__pubsub_publish", PublishInput(...))
```

My earlier claim that batching would be "awkward" with updates was wrong.

### Return value

Updates can return a result. A publish-via-update could return the assigned
offsets, confirmation of delivery, or the current log length. With signals,
the client has no way to learn the outcome without a separate query.

### Event history cost

Each signal adds `WorkflowSignalReceived` to history (1 event). Each update
adds `WorkflowExecutionUpdateAccepted` + `WorkflowExecutionUpdateCompleted`
(2 events). Updates consume history faster, bringing CAN sooner.

### Concurrency limits

Temporal Cloud has [per-workflow update limits](https://docs.temporal.io/cloud/limits#per-workflow-execution-update-limits).
Signals have no equivalent limit. For very high-throughput scenarios, signals
may be the only option.

## Recommendation

**Keep signals for publishing.** The non-blocking property is the decisive
factor for the streaming use case. The application-level dedup
(`publisher_id` + `sequence`) is a permanent requirement for signals and
is already implemented with TLA+ verification.

**Alternative worth revisiting**: If the non-blocking property were less
important (e.g., lower-throughput use case), updates would be attractive.
Once temporal/temporal#6375 is fixed, update-based publishing with CAN
would get platform-native exactly-once with no application dedup needed.
The tradeoff is blocking flush + 2x history events per batch.

For the current streaming use case, signals remain the right choice.

**Keep updates for polling.** The `__pubsub_poll` update is the correct
choice for subscription: the caller needs a result (the items), and blocking
is the desired behavior (long-poll semantics).

## What Would Change If We Switched

For completeness, here's what a switch to update-based publishing would
require:

1. Replace signal handler `__pubsub_publish` with an update handler
2. The publish handler becomes synchronous (just appends to log) — fast
3. Client flush changes from `handle.signal(...)` to
   `handle.execute_update(...)`
4. Background flusher blocks on the update call instead of fire-and-forget
5. Application-level dedup stays (CAN requirement)
6. Update validator could reject publishes during drain (already done for
   polls)
7. Return type could include assigned offsets

The dedup protocol, TLA+ specs, and mixin-side handler logic would be
essentially unchanged. The change is mechanical, not architectural.

## Signal Ordering Guarantee

Temporal guarantees that signals from a single client, sent sequentially
(each signal call completes before the next is sent), are delivered in order:

> "Signals are delivered in the order they are received by the Cluster and
> written to History."
> ([docs](https://docs.temporal.io/workflows#signal))

The guarantee breaks down only for *concurrent* signals — if two signal RPCs
are in flight simultaneously, their order in history is nondeterministic.

The pub/sub client's `_flush_lock` ensures signals are never sent
concurrently from a single `PubSubClient` instance. The sequence is:

1. Acquire lock
2. `await handle.signal(...)` — blocks until server writes to history
3. Release lock

This means batches from a single publisher are ordered in the workflow log.
Combined with the workflow's single-threaded signal processing (the
`_pubsub_publish` handler is synchronous — no `await`), items within and
across batches preserve their publish order.

**Cross-publisher ordering** is nondeterministic. If publisher A and
publisher B send signals concurrently, the interleaving in history depends
on arrival order at the server. Within each publisher's stream, ordering is
preserved. This matches the `OrderPreservedPerPublisher` invariant verified
in `PubSubDedupTTL.tla`.

## Sources

- [Temporal docs: Message handler patterns — exactly-once processing](https://docs.temporal.io/handling-messages#exactly-once-message-processing)
- [Temporal docs: Signals vs Updates decision table](https://docs.temporal.io/encyclopedia/workflow-message-passing)
- [temporal/temporal#6375: CompleteUpdate not honored during CAN](https://github.com/temporalio/temporal/issues/6375)
- [Community: Deduping workflow signals](https://community.temporal.io/t/deduping-workflow-signals/5547)
- [Community: Idempotent signals investigation](https://community.temporal.io/t/preliminary-investigation-into-idempotent-signals/13694)
- [Slack: request_id is for client call dedup, not application dedup](https://temporalio.slack.com/archives/C012SHMPDDZ/p1729554260821239)
