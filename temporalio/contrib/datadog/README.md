# Datadog Tracing Interceptor for Temporal Python

## Background

### Why this is more complex than Go

The Python Temporal SDK does not provide a common tracing interface to
implement. Instead, it exposes interceptor base classes that users extend to
inject behaviour at each operation boundary. This is idiomatic Python but
makes integration more involved than the Go equivalent.

The more significant challenge is how the Python SDK executes workflow code.
Workflows run inside a sandboxed environment where the worker re-imports the
workflow module before every execution. The sandbox also enforces
determinism constraints, so tracing logic (which depends on `ddtrace`, a
module that schedules asyncio work during import) cannot live directly inside
it.

### Difference from the upstream OpenTelemetry interceptor

The upstream OpenTelemetry tracing interceptor works around the sandbox
limitation by emitting a zero-duration notification span whenever a workflow
execution occurs. Activities and other events attach to that span. While
functional, this approach does not produce actual workflow traces: the
`RunWorkflow` span has no duration and the trace does not survive a worker
restart.

This implementation instead generates real workflow traces. The `RunWorkflow`
span starts when the first worker picks up the workflow and finishes only when
the workflow completes, matching Go's behaviour. Deterministic span IDs and
context propagation ensure the trace remains coherent even if a worker
restarts mid-execution.

## Usage

```python
import ddtrace
from temporalio.client import Client
from temporalio.contrib.datadog import DatadogTracingInterceptor

# Inject dd.trace_id and dd.span_id into every log record so workflow and
# activity logs are correlated with their trace in Datadog Log Management.
ddtrace.patch(logging=True)

interceptor = DatadogTracingInterceptor(
    service_name="my-service",
    extra_tags={"deployment.environment": "prod"},
)
client = await Client.connect("localhost:7233", interceptors=[interceptor])
```

**Important**: Passing the `interceptor` instance to the client is enough.
The worker will automatically pick up the interceptor from the client.

## Deterministic span IDs

The workflows' `RunWorkflow` spans may be long-running. If the worker
restarts and the workflow is replayed, the new execution recreates
the span with the **same span ID** so the trace remains coherent in APM.

Span IDs for `RunWorkflow` (and any operation with an idempotency key) are
derived via FNV-1 64-bit hash of a key:

```
WorkflowInboundInterceptor:<namespace>:<workflow_id>:<run_id>:<span_counter>
```

This matches the Go SDK's algorithm byte-for-byte, so a workflow started by a
Go client and executed by a Python worker produces the same span ID. The
counter starts at 1 (reserved for RunWorkflow) and increments for each
subsequent handler span (HandleSignal, HandleUpdate) to give each a stable,
unique ID across worker restarts.

## Replay safety

Temporal replays workflow history on every new worker to rebuild execution
state. Without guards, replay would re-emit duplicate completed spans for
operations that already finished on the dead worker.

Two mechanisms prevent duplicates:

**Inbound handlers** (HandleSignal, HandleUpdate): suppressed during replay
via `temporalio.workflow.unsafe.is_replaying()`. A non-`None` idempotency key
signals that the span completed within a single workflow task and must not be
re-emitted. RunWorkflow is explicitly exempt — its span is in-flight and was
never sent by the dead worker, so the new worker recreates it.

**Outbound operations** (StartActivity, StartLocalActivity, StartChildWorkflow,
SignalChildWorkflow, SignalExternalWorkflow): suppressed during replay by an
early `is_replaying()` check at the top of each outbound interceptor method.
The Temporal SDK matches these commands against history and returns cached
results, but the interceptor code runs first — without the guard, a fresh span
would be emitted for every replayed command.

Queries and update validators are never suppressed: queries are not in
history (they run on demand), and validators do not execute during replay.

## Workflow sandbox

Temporal runs workflow code inside a restricted import sandbox. Importing
`ddtrace` from within that import path triggers an asyncio-loop conflict
during ddtrace init and a `builtins.open` restriction from pytest's assertion
rewriter.

The interceptor works around this with an extern-function bridge:

1. `DatadogTracingInterceptor` (host side) registers functions under
   `unsafe_extern_functions` before the sandbox starts.
2. `DatadogTracingWorkflowInboundInterceptor` (sandbox side) retrieves those
   functions via `temporalio.workflow.extern_functions()` at init time and
   holds them as instance attributes.
3. All ddtrace calls (`start_span`, `finish_span`, baggage, annotation) go
   through these externs, so the sandbox never imports ddtrace directly.

Two `contextvars.ContextVar` values live on the host module and are accessed
by the sandbox exclusively through externs:

- `_active_workflow_span` — the live `RunWorkflow` ddtrace span for the
  current execution. Used by outbound operations to parent their spans to
  RunWorkflow when no propagated header is available.
- `_trace_disconnected` — set by `disconnect_trace_span_from_workflow_context`
  to suppress trace propagation into the next ContinueAsNew run.

## ContinueAsNew

`_WorkflowOutboundInterceptor.continue_as_new` injects the current RunWorkflow
span context into the ContinueAsNew headers so the next run's RunWorkflow span
is a child of the current one, forming a continuous trace across runs.

Call `disconnect_trace_span_from_workflow_context()` before
`workflow.continue_as_new()` to start a fresh root trace for the next run
instead.
