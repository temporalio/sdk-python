# Dynamic-signal vs. class-level-update race in `contrib/pubsub`

**Status:** design note for team review.
**Context:** surfaced while stabilizing PR #1423 CI; one test
(`test_poll_truncated_offset_returns_application_error`) failed
deterministically under parallel load before being patched around.

## TL;DR

`PubSub` registers `__pubsub_publish` as a **dynamic** signal handler
(via `workflow.set_signal_handler` inside `PubSub.__init__`). Any
**class-level, synchronous** `@workflow.update` that reads `PubSub`
state and fires in the **same activation** as a just-arrived
`__pubsub_publish` signal will observe pre-signal state — zero items
in the log — and raise from the handler before the buffered signal
task gets a chance to run.

The PR works around this by seeding log state from `@workflow.init`
in the test workflow. That keeps CI green but does not fix the race
for users who follow the pattern `handle.signal(...)` → synchronous
user update. We document the gotcha and publish a one-line recipe
(`await asyncio.sleep(0)` at the top of sync update handlers that
read `PubSub` state); see the Recommendation section.

## How the race is triggered

Consider a workflow using `PubSub` with a user-defined synchronous
update that reads the log:

```python
@workflow.defn
class MyWorkflow:
    @workflow.init
    def __init__(self) -> None:
        self.pubsub = PubSub()

    @workflow.update            # class-level, synchronous
    def truncate(self, offset: int) -> None:
        self.pubsub.truncate(offset)

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)
```

And a client that publishes then immediately truncates:

```python
handle = await client.start_workflow(MyWorkflow.run, ...)
await handle.signal("__pubsub_publish", PublishInput(items=[...5 items...]))
await handle.execute_update("truncate", 3)
```

Under parallel test load (or just bad luck on the server), all three
events — `InitializeWorkflow`, `SignalWorkflow(__pubsub_publish)`,
`DoUpdate(truncate)` — can arrive at the worker in a **single**
`WorkflowActivation`.

### What the worker does with that activation

From `temporalio/worker/_workflow_instance.py`:

1. `activate()` groups jobs into buckets
   (`_workflow_instance.py:440–455`):
   - `job_sets[1]` = signals **and** updates
   - `job_sets[2]` = initialize_workflow, activity resolutions, etc.

2. Process `job_sets[1]` (signals + updates) **first**
   (`_workflow_instance.py:461–466`):
   - `_apply(Signal(__pubsub_publish))` → looks up `self._signals`.
     `__pubsub_publish` is registered **dynamically inside
     `PubSub.__init__`**, which has not run yet. No handler → signal
     goes into `_buffered_signals`
     (`_workflow_instance.py:1061–1063`).
   - `_apply(Update(truncate))` → looks up `self._updates`. `truncate`
     is a **class-level** `@workflow.update`, so it is present in
     `self._updates` from the workflow instance context's `__init__`
     (`self._updates = dict(self._defn.updates)` at
     `_workflow_instance.py:316`). A task is created immediately
     and scheduled via `loop.call_soon`, appending to `self._ready`
     (`_apply_do_update` → `create_task` at
     `_workflow_instance.py:721`).

3. `_run_once` (`_workflow_instance.py:2478–2511`):
   - Lazy-instantiate the workflow object
     (`_workflow_instance.py:2485–2486`). This runs `__init__`
     **synchronously**. `PubSub.__init__` calls
     `workflow.set_signal_handler("__pubsub_publish", self._on_publish)`.
   - `workflow_set_signal_handler` (`_workflow_instance.py:1401–1424`)
     installs the handler **and immediately drains the buffer** —
     dispatching each buffered signal job through
     `_process_signal_job`
     (`_workflow_instance.py:2415–2453`), which creates an `asyncio.Task`.
     That task's first `__step` lands in `self._ready` — **after** the
     update task already there.
   - The event loop drains `self._ready` in FIFO order
     (`_workflow_instance.py:2489–2493`):
     - **Update task runs first.** `truncate(3)` sees `self._log == []`.
     - **Signal task runs second.** `_on_publish` appends 5 items —
       too late.

4. Before this PR, the update handler raised `ValueError` on the
   empty-log check. That is not an `ApplicationError`, so it fails
   the **entire workflow task**, not just the update. Subsequent
   `execute_update("__pubsub_poll", …)` then returns
   `WorkflowNotReadyFailure` and the test aborts.

### Why `__pubsub_poll` is not affected

`_on_poll` is `async` and contains

```python
await workflow.wait_condition(
    lambda: len(self._log) > log_offset or self._draining,
)
```

Even if the poll task runs before the signal task, the first `await`
yields back to the loop, the buffered-signal task gets its turn,
`_log` gets populated, the condition unblocks, and poll returns
items. The race is invisible for async handlers that yield.

## Who is affected

A user workflow hits this iff **all** are true:
- The workflow uses `PubSub` (so `__pubsub_publish` is dynamic).
- The workflow defines a class-level `@workflow.update` or
  `@workflow.signal` that reads `PubSub` state synchronously (no
  `await`).
- A client issues `handle.signal("__pubsub_publish", …)` immediately
  followed by a call to that sync update, and the server batches
  init + signal + update into one activation.

The module's own `__pubsub_poll` avoids it (async). Workflow-internal
publishes (`self.pubsub.publish(...)` from `run()` or an activity)
avoid it (no client-initiated signal race). The failure mode is a
narrow slice but very real: it reproduced deterministically under
`pytest -n auto` load in CI and locally.

## Zooming out: this race is a subset of a broader concern

In most real applications that use `PubSub`, the publisher and the
caller of any custom update/query are independent actors — a
producer service publishes; a control-plane client reads or mutates.
From the update handler's perspective, these scenarios are
indistinguishable:

1. **SDK race.** Publish buffered in the same activation as the
   update; signal handler not yet installed; update reads pre-signal
   state.
2. **Network race.** Publish hasn't reached the server yet; update
   arrives first.
3. **Genuinely early / out-of-range.** Publish is never coming; the
   caller passed a bad offset.

All three surface to the handler as "log is shorter than what the
caller asked about." Any handler that is robust to (2) and (3) —
which it must be, because those are inherent to distributed systems
— is automatically robust to (1). Whatever policy the handler picks
for "asked to act on state that isn't here yet" (error, wait with
timeout, no-op) covers the SDK race too.

The case where "application robustness is enough" breaks down is
**sequential same-client ordering**:

```python
await handle.signal("__pubsub_publish", items)     # awaited
await handle.execute_update("custom_op", ...)      # expects items visible
```

Here the caller completed the signal before issuing the update and
reasonably expects ordering to hold. The SDK race violates that
expectation. In practice, this single-client shape is rare in
`PubSub` use — the whole module shape is "one side writes, a
different side reads/mutates." Callers who *do* depend on sequential
ordering should use the recipe in the Recommendation section.

## Options

### 1. Do nothing (leave the PR's test-only workaround)

**What:** keep `prepub_count` seeding in `TruncateWorkflow.__init__`.
Tests pass. Users with the affected pattern still hit the race.

**Pros:** zero extra work, unblocks #1423.
**Cons:** silent footgun for users. Likely to resurface as a support
ticket.

### 2. Document the caveat with a concrete recipe

**What:** add a section to the `PubSub` docstring / contrib README
with the specific fix:

> Custom synchronous `@workflow.update` or `@workflow.signal`
> handlers that read `PubSub` state seeded by `__pubsub_publish`
> may observe stale state when the external signal and the custom
> handler arrive in the same workflow activation. To close the
> window, make the handler `async` and yield once before touching
> `PubSub` state:
>
> ```python
> import asyncio
>
> @workflow.update
> async def my_update(self, ...) -> None:
>     await asyncio.sleep(0)       # let pending __pubsub_publish apply
>     self.pubsub.truncate(...)    # now sees post-signal state
> ```
>
> `asyncio.sleep(0)` is a pure asyncio-level yield — no Temporal
> timer, no history events, no server round trip. Do not use
> `workflow.sleep(0)` (that *does* schedule a timer).
>
> Already-safe patterns: async handlers that `await` anything
> (including `workflow.wait_condition`); the module's own
> `__pubsub_poll`; any handler whose semantics already include
> "wait for the state I'm asking about" (use `wait_condition` on a
> meaningful predicate).

**Pros:** honest; cheap; steers users toward a concrete, correct
pattern. Recipe matches what the SDK-level fix would do implicitly.
**Cons:** still a sharp edge — relies on users reading. See the
"Zooming out" section above: most applications have to be robust to
the same out-of-order arrival for reasons unrelated to this race,
so the recipe is only needed when users rely on strict sequential
same-client ordering.

### 3. Make `__pubsub_publish` class-level (revert to a mixin)

**What:** undo 72d296ea — expose `PubSubMixin` with
`@workflow.signal def __pubsub_publish(...)`. Users opt in by
inheritance. Class-level signals are present in `self._signals` from
instance-context construction, so `_apply(Signal)` schedules a
**signal** task, not buffers, and FIFO dispatch runs signal before
update.

**Pros:** fully fixes the race at the library layer with no SDK
change. Zero user-visible footgun.
**Cons:** reintroduces all the reasons we moved to dynamic:
multiple-inheritance conflicts, users forgetting to inherit,
awkward composition with other mixins, forced class hierarchy.
We already rejected this.

### 4. Fix the dispatch order in the SDK

**What:** in `workflow_set_signal_handler` (or in `_run_once`),
arrange for buffered-signal tasks to be dispatched **ahead of** any
update tasks already queued from `_apply(job_sets[1])`. Concretely,
either:

- Run buffered signal handlers synchronously (no `create_task`) when
  drained from the buffer during `set_signal_handler`, so their state
  mutations land before any task in `self._ready` runs; or
- Swap the grouping in `activate()` so `initialize_workflow` is
  applied before signals+updates — so `PubSub.__init__` runs, the
  signal handler is live at `_apply(Signal)` time, and the signal
  task is created before the update task.

**Pros:** real fix. Benefits every dynamic-signal user, not just
`PubSub`. Preserves current PubSub API.
**Cons:** non-trivial SDK change with broader blast radius.
Needs design review, wider test coverage (queries, continue-as-new,
updates with validators, async signals…). Not something we ship
alongside this PR.

### 5. Make `PubSub.truncate` require an async context / add a publisher barrier

**What:** explicitly disallow sync updates reading `PubSub` state by
making the read-path APIs async — e.g., `await pubsub.truncate(...)`
that internally `wait_condition`s on a "signal handler at least N
times" barrier. Or expose a `await pubsub.wait_for_publish_applied()`
primitive users call at the top of sync updates (which makes them
no longer sync, defeating the purpose).

**Pros:** race-safe if users follow the API.
**Cons:** leaky — pushes SDK-activation-ordering concerns into the
user API. Compromises ergonomics of what should be a simple
in-memory mutation.

## Recommendation

Ship **(1) + (2)** now. Treat **(4)** as optional follow-up, not a
blocker.

- Keep the `prepub_count` change in the test (it is legitimate test
  scaffolding and avoids baking SDK-ordering assumptions into the
  test surface).
- Add the caveat + `asyncio.sleep(0)` recipe from option (2) to the
  contrib README as a visible "Gotcha" section, not a footnote, with
  a link to this document for the full mechanics.
- Optionally file an issue against sdk-python for option (4). It is
  a principled fix (dispatch buffered signals ahead of updates on
  the same activation, or reorder the job-set buckets) but given
  the "Zooming out" analysis, the payoff is narrow: it only helps
  users who rely on sequential same-client publish→update ordering,
  which is an uncommon pattern for `PubSub`.

Rationale:
- Applications using `PubSub` with independent producers and
  consumers must already handle "update arrives before publish" as
  a general concern — the SDK race is a narrow special case covered
  by that same robustness.
- (3) reverses a deliberate API decision we already made.
- (4) is correct but is a core-sdk-behavior change that deserves its
  own PR, reviewers, and wider-test coverage (queries,
  continue-as-new, validators, async signals…); the benefit is
  limited to the sequential-same-client case.
- (5) bleeds SDK internals into user API.
- (1) alone is not enough — we need (2) so the escape hatch is
  discoverable by users who do depend on sequential ordering.

## Appendix: Minimal repro (already in the test file, pre-patch)

```python
@workflow.defn
class TruncateWorkflow:
    @workflow.init
    def __init__(self) -> None:
        self.pubsub = PubSub()

    @workflow.update
    def truncate(self, offset: int) -> None:    # sync
        self.pubsub.truncate(offset)

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)

# client
handle = await client.start_workflow(TruncateWorkflow.run, ...)
await handle.signal("__pubsub_publish", PublishInput(items=[...5 items...]))
await handle.execute_update("truncate", 3)   # racy
```

Under `pytest -n auto --dist=worksteal` the update reliably observes
`len(self._log) == 0` and fails the workflow task. Running the test
in isolation passes every time.
