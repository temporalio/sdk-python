# Continue-As-New Addendum

Addendum to [DESIGN.md](./DESIGN.md). Addresses the continue-as-new (CAN) gap
identified in section 10 ("Event retention").

## Problem

The pub/sub mixin accumulates workflow history through two channels:

1. **Signals** — each `__pubsub_publish` signal adds a `WorkflowSignaled` event
   plus the serialized `PublishInput` payload.
2. **Updates** — each `__pubsub_poll` response serializes the returned
   `PollResult` (including all matched items) into the history as an update
   completion event.

Over a streaming agent session, a subscriber polling every few seconds
accumulates many update-completion events, each containing a slice of the log.
These are redundant copies of data already held in `_pubsub_log`. The history
grows toward the ~50K event warning threshold, at which point Temporal forces
termination.

Continue-as-new resets the history. By serializing the full log into the CAN
input, we carry a single canonical copy forward and discard all the redundant
history entries from prior signals, updates, and queries.

## Design

### `PubSubState` type

New dataclass in `_types.py`:

```python
@dataclass
class PubSubState:
    """Serializable snapshot of pub/sub state for continue-as-new."""
    log: list[PubSubItem] = field(default_factory=list)
```

The offset counter is not stored — it is derived as `len(log)`. This avoids
any possibility of the counter and log diverging.

Exported from `__init__.py`.

### Mixin changes

New and modified methods on `PubSubMixin`:

```python
def init_pubsub(self, prior_state: PubSubState | None = None) -> None:
    """Initialize pub/sub state.

    Args:
        prior_state: State from a previous run (via get_pubsub_state()).
                     Pass None on the first run.
    """
    if prior_state is not None:
        self._pubsub_log = list(prior_state.log)
    else:
        self._pubsub_log = []
    self._pubsub_draining = False

def get_pubsub_state(self) -> PubSubState:
    """Return a serializable snapshot of pub/sub state.

    Call this when building your continue-as-new arguments.
    """
    return PubSubState(log=list(self._pubsub_log))
```

The mixin does **not** trigger CAN itself. The parent workflow decides when to
continue-as-new (typically by checking `workflow.info().is_continue_as_new_suggested()`
at a safe point in its main loop).

### Draining: `drain_pubsub()` + update validator

A long-poll `__pubsub_poll` handler can block for up to 300 seconds waiting for
new items. We cannot let that block continue-as-new indefinitely. Conversely, a
naive drain that unblocks waiting polls but doesn't reject new ones creates a
race: the client receives an empty result, immediately sends a new poll, the new
poll is accepted, and `all_handlers_finished()` never stabilizes. This is
because `await workflow.wait_condition(workflow.all_handlers_finished)` yields,
allowing the SDK to process new events — including new update acceptances — in
the same or subsequent workflow tasks.

The solution is two mechanisms working together:

1. **A drain flag** that unblocks all waiting poll handlers.
2. **An update validator** that rejects new polls once draining is set.

```python
def drain_pubsub(self) -> None:
    """Unblock all waiting poll handlers and reject new polls.

    Call this before waiting for all_handlers_finished() and
    continue_as_new().
    """
    self._pubsub_draining = True

@workflow.update(name="__pubsub_poll")
async def _pubsub_poll(self, input: PollInput) -> PollResult:
    await workflow.wait_condition(
        lambda: len(self._pubsub_log) > input.from_offset
                or self._pubsub_draining,
        timeout=input.timeout,
    )
    # Return whatever items are available (possibly empty if drain-only)
    all_new = self._pubsub_log[input.from_offset:]
    next_offset = len(self._pubsub_log)
    if input.topics:
        topic_set = set(input.topics)
        filtered = [item for item in all_new if item.topic in topic_set]
    else:
        filtered = list(all_new)
    return PollResult(items=filtered, next_offset=next_offset)

@_pubsub_poll.validator
def _validate_pubsub_poll(self, input: PollInput) -> None:
    if self._pubsub_draining:
        raise RuntimeError("Workflow is draining for continue-as-new")
```

The validator is read-only (checks a flag, raises to reject) — this satisfies
the Temporal constraint that validators must not mutate state or block.

**CAN sequence in the parent workflow:**

```python
self.drain_pubsub()
await workflow.wait_condition(workflow.all_handlers_finished)
workflow.continue_as_new(args=[...])
```

What happens:

1. `drain_pubsub()` sets `_pubsub_draining = True`.
2. All blocked `__pubsub_poll` handlers unblock (the `or self._pubsub_draining`
   clause becomes true) and return their current items.
3. The validator rejects any new `__pubsub_poll` updates — they are never
   accepted, so no new handlers start.
4. `all_handlers_finished()` becomes true and **stays** true.
5. `continue_as_new()` proceeds.

On the client side, the rejected poll surfaces as an error. The subscriber
detects CAN via `describe()`, follows the chain, and resumes from the same
offset against the new run.

### Client-side CAN resilience

The current `subscribe()` method catches `CancelledError` and
`WorkflowUpdateRPCTimeoutOrCancelledError`, then stops iteration. It has no
CAN awareness.

#### New behavior

`subscribe()` gains a `follow_continues` parameter (default `True`):

```python
async def subscribe(
    self,
    topics: list[str] | None = None,
    from_offset: int = 0,
    *,
    follow_continues: bool = True,
) -> AsyncIterator[PubSubItem]:
```

When an `execute_update` call fails and `follow_continues` is `True`, the
client:

1. Calls `describe()` on the current handle to check execution status.
2. If the status is `CONTINUED_AS_NEW`, replaces `self._handle` with a fresh
   handle for the same workflow ID (no pinned `run_id`), then retries the poll
   from the same offset.
3. If the status is anything else, re-raises the original error.

```python
async def _follow_continue_as_new(self) -> bool:
    """Check if the workflow continued-as-new and update the handle.

    Returns True if the handle was updated (caller should retry).
    """
    try:
        desc = await self._handle.describe()
    except Exception:
        return False
    if desc.status == WorkflowExecutionStatus.CONTINUED_AS_NEW:
        self._handle = self._handle._client.get_workflow_handle(
            self._handle.id
        )
        return True
    return False
```

The retry succeeds because the new run's log contains all items from the
previous run. Polling from the same offset returns the expected items.

#### Why this works with `activity_pubsub_client()`

`activity_pubsub_client()` creates handles via
`activity.client().get_workflow_handle(workflow_id)` — no `run_id` pinned.
Signals and updates already route to the current run, so activity-side
publishing is CAN-friendly without changes.

## Offset Continuity

Since the full log is carried forward:

- Pre-CAN: offsets `0..N-1`, `len(log) == N`.
- Post-CAN: `init_pubsub(prior_state)` restores the same N items. New appends
  start at offset N.
- A subscriber at offset K (where K < N) polls the new run and gets items
  `K..N-1` from the carried-forward log, then continues with new items.

No offset remapping. No sentinel values. No coordination protocol.

## Usage Example

```python
@dataclass
class WorkflowInput:
    # ... application fields ...
    pubsub_state: PubSubState | None = None

@workflow.defn
class AgentWorkflow(PubSubMixin):
    @workflow.run
    async def run(self, input: WorkflowInput) -> None:
        self.init_pubsub(prior_state=input.pubsub_state)

        while True:
            await workflow.wait_condition(
                lambda: self._pending_message or self._closed
            )
            if self._closed:
                return

            await self._run_turn(self._pending_message)

            if workflow.info().is_continue_as_new_suggested():
                self.drain_pubsub()
                await workflow.wait_condition(workflow.all_handlers_finished)
                workflow.continue_as_new(args=[WorkflowInput(
                    # ... application fields ...
                    pubsub_state=self.get_pubsub_state(),
                )])
```

## Edge Cases

### Payload size limit

The full log serialized into CAN input could approach Temporal's default 2 MB
payload limit for very long sessions with large payloads. This is an inherent
constraint of the full-history approach.

Mitigation: the snapshot + truncate extension described in DESIGN.md section 10
addresses this by discarding consumed entries before CAN. That extension becomes
the natural next step if payload size becomes a problem in practice.

### Signal delivery during CAN

A `PubSubClient` in publish mode sending signals mid-CAN may get errors if
its handle is pinned to the old run. The publishing side does **not**
auto-follow CAN — the parent workflow should ensure activities complete (and
therefore stop publishing) before triggering CAN.

### Concurrent subscribers

Multiple subscribers independently follow the CAN chain. Each maintains its
own offset. Sharing a `PubSubClient` instance across concurrent `subscribe()`
calls is safe — they all want to target the latest run, and the handle is
effectively just a workflow ID reference.
