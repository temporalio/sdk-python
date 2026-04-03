# LangSmith Plugin for Temporal Python SDK

This Temporal [plugin](https://docs.temporal.io/develop/plugins-guide) allows your [LangSmith](https://smith.langchain.com/) traces to be fully replay safe when added to Temporal workflows and activities. It propagates trace context across worker boundaries so that `@traceable` calls, LLM invocations, and Temporal operations show up in a single connected trace, and ensures that replaying does not generate duplicate traces.

## Quick Start

Register the plugin on your Temporal client. You need it on both the client (starter) side and the workers:

```python
from temporalio.client import Client
from temporalio.contrib.langsmith import LangSmithPlugin

client = await Client.connect(
    "localhost:7233",
    plugins=[LangSmithPlugin(project_name="my-project")],
)
```

Once that's set up, any `@traceable` function inside your workflows and activities will show up in LangSmith with correct parent-child relationships, even across worker boundaries.

## Example: AI Chatbot

A conversational chatbot using OpenAI, orchestrated by a Temporal workflow. The workflow stays alive waiting for user messages via signals, and dispatches each message to an activity that calls the LLM.

### Activity (wraps the LLM call)

```python
@langsmith.traceable(name="Call OpenAI", run_type="chain")
@activity.defn
async def call_openai(request: OpenAIRequest) -> Response:
    client = wrap_openai(AsyncOpenAI()) # This is a traced langsmith function
    return await client.responses.create(
        model=request.model,
        input=request.input,
        instructions=request.instructions,
    )
```

### Workflow (orchestrates the conversation)

```python
@workflow.defn
class ChatbotWorkflow:
    @workflow.run
    async def run(self) -> str:
        # @traceable works inside workflows — fully replay-safe
        now = workflow.now().strftime("%b %d %H:%M")
        return await langsmith.traceable(
            name=f"Session {now}", run_type="chain",
        )(self._session)()

    async def _session(self) -> str:
        while not self._done:
            await workflow.wait_condition(
                lambda: self._pending_message is not None or self._done
            )
            if self._done:
                break

            message = self._pending_message
            self._pending_message = None

            @langsmith.traceable(name=f"Request: {message[:60]}", run_type="chain")
            async def _query(msg: str) -> str:
                response = await workflow.execute_activity(
                    call_openai,
                    OpenAIRequest(model="gpt-4o-mini", input=msg),
                    start_to_close_timeout=timedelta(seconds=60),
                )
                return response.output_text

            self._last_response = await _query(message)

        return "Session ended."
```

### Worker

```python
client = await Client.connect(
    "localhost:7233",
    plugins=[LangSmithPlugin(project_name="chatbot")],
)

worker = Worker(
    client,
    task_queue="chatbot",
    workflows=[ChatbotWorkflow],
    activities=[call_openai],
)
await worker.run()
```

### What you see in LangSmith

With the default configuration (`add_temporal_runs=False`), the trace only contains your application logic:

```
Session Apr 03 14:30
  Request: "What's the weather in NYC?"
    Call OpenAI
      openai.responses.create  (auto-traced by wrap_openai)
```

<!-- Screenshot: LangSmith trace tree with add_temporal_runs=False showing clean application-only hierarchy -->

## `add_temporal_runs` — Temporal Operation Visibility

By default, `add_temporal_runs` is `False` and only your `@traceable` application logic appears in traces. Setting it to `True` also adds Temporal operations (StartWorkflow, RunWorkflow, StartActivity, RunActivity, etc.):

```python
plugins=[LangSmithPlugin(project_name="my-project", add_temporal_runs=True)]
```

This adds Temporal operation nodes to the trace tree so that the orchestration layer is visible alongside your application logic. If the caller wraps `start_workflow` in a `@traceable` function, the full trace looks like:

```
Ask Chatbot                      # @traceable wrapper around client.start_workflow
  StartWorkflow:ChatbotWorkflow
  RunWorkflow:ChatbotWorkflow
    Session Apr 03 14:30
      Request: "What's the weather in NYC?"
        StartActivity:call_openai
        RunActivity:call_openai
          Call OpenAI
            openai.responses.create
```

Note: `StartFoo` and `RunFoo` appear as siblings. The start is the short-lived outbound RPC that completes immediately, and the run is the actual execution which may take much longer.

<!-- Screenshot: LangSmith trace tree with add_temporal_runs=True showing Temporal operation nodes -->

<!-- Screenshot: Temporal UI showing the corresponding workflow execution -->

## Migrating Existing LangSmith Code to Temporal

If you already have code with LangSmith tracing, you should be able to move it into a Temporal workflow and keep the same trace hierarchy. The plugin handles sandbox restrictions and context propagation behind the scenes, so anything that was traceable before should remain traceable after the move. More details below:

### Where `@traceable` works

The plugin allows `@traceable` to work inside Temporal's deterministic workflow sandbox, where it normally can't run:

| Location | Works? | Notes |
|----------|--------|-------|
| On `@activity.defn` functions | Yes | Stack `@traceable` on top of `@activity.defn` |
| On `@workflow.defn` class | No | Use `@traceable` inside `@workflow.run` instead |
| Inside workflow methods (sync or async) | Yes | Use `langsmith.traceable(name="...")(fn)()` |
| Inside activity methods (sync or async) | Yes | Regular `@traceable` decorator |
| Around `client.start_workflow` / `execute_workflow` | Yes | Wrap the caller to trace the entire workflow as one unit |
| Around `execute_activity` calls | Yes | Wrap the dispatch to group related operations |

## Replay Safety

Temporal workflows are deterministic and get replayed from event history on recovery. The plugin accounts for this by injecting replay-safe data into your traceable runs:

- **No duplicate traces on replay.** Run IDs are derived deterministically from the workflow's random seed, so replayed operations produce the same IDs and LangSmith deduplicates them.
- **No non-deterministic calls.** The plugin injects metadata using `workflow.now()` for timestamps and `workflow.random()` for UUIDs instead of `datetime.now()` and `uuid4()`.
- **Background I/O stays outside the sandbox.** LangSmith HTTP calls to the server are submitted to a background thread pool that doesn't interfere with the deterministic workflow execution.

You don't need to do anything special for this. Your `@traceable` functions behave the same whether it's a fresh execution or a replay.

### Example: Worker crash mid-workflow

```
1. Workflow starts, executes Activity A          -> trace appears in LangSmith
2. Worker crashes
3. New worker picks up the workflow
4. Workflow replays Activity A (skips execution) -> NO duplicate trace
5. Workflow executes Activity B (new work)       -> new trace appears
```

<!-- Screenshot: LangSmith showing a workflow trace that survived a worker restart with no duplicate runs -->

### Example: Wrapping retriable steps in a trace

Since Temporal retries failed activities, you can use `@traceable` to group the attempts together:

```python
@langsmith.traceable(name="my_step", run_type="chain")
async def my_step(message: str) -> str:
    return await workflow.execute_activity(
        call_openai,
        ...
    )
```

This groups everything under one run:
```
my_step
  Call OpenAI           # first attempt
    openai.responses.create
  Call OpenAI           # retry
    openai.responses.create
```

## Context Propagation

The plugin propagates trace context across process boundaries (client -> workflow -> activity -> child workflow -> nexus) via Temporal headers. You don't need to pass any context manually.

```
Client Process              Worker Process (Workflow)        Worker Process (Activity)
─────────────              ──────────────────────────       ─────────────────────────
@traceable("my workflow")
  start_workflow ──headers──> RunWorkflow
                               @traceable("session")
                                 execute_activity ──headers──> RunActivity
                                                                @traceable("Call OpenAI")
                                                                  openai.create(...)
```

## API Reference

### `LangSmithPlugin`

```python
LangSmithPlugin(
    client=None,           # langsmith.Client instance (auto-created if None)
    project_name=None,     # LangSmith project name
    add_temporal_runs=False,  # Show Temporal operation nodes in traces
    metadata=None,         # Default metadata for all runs
    tags=None,             # Default tags for all runs
)
```

We recommend registering the plugin on both the client and all workers. Strictly speaking, you only need it on the sides that produce traces, but adding it everywhere avoids surprises with context propagation. The client and worker don't need to share the same configuration — for example, they can use different `add_temporal_runs` settings.
