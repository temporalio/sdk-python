# Streams helpers

> This package is experimental. Its API can change in a later release.

`temporalio.contrib.streams` holds helpers that sit on top of the Streams API of the SDK. A
helper takes a client `Stream` or a Workflow `Stream`.

## `BufferedAppender`

`BufferedAppender` batches the appends of one Stream. `append` returns immediately, and the
appender writes the buffered Records as one batch. In a client or an Activity a batch waits
at most `max_delay`. In Workflow code a batch starts when the code next yields, so it adds no
timer to History.

```python
from temporalio.contrib.streams import BufferedAppender

async with BufferedAppender(tokens) as appender:
    for chunk in chunks:
        appender.append(chunk)
```

The end of the `async with` block writes the Records that are still in the buffer.

## A stored Producer

`current_producer` gives the Producer that an append of the calling code sends when it passes
no `producer=`. An Activity that resumes from its heartbeat details can store it there, and its
retry can write as the same Producer. Heartbeat details give the Producer back as its proto
message, so `as_producer` turns it back into a `Producer`.

```python
@activity.defn
async def write_parts(ref: StreamRef[str], parts: list[str]) -> None:
    details = activity.info().heartbeat_details
    producer = as_producer(details[0]) if details else current_producer()
    stream = activity.client().get_stream(ref, body_type=str)
    for index in range(details[1] if details else 0, len(parts)):
        await stream.append(parts[index], producer=producer)
        activity.heartbeat(producer, index + 1)
```

The retry's Records continue the first attempt's epoch, so a reader does not take them as a
replacement of the first attempt's Records.

## Reads and appends in Workflow code

The helpers run on the Streams API, so they follow how Workflow code reads, appends and
closes. On a Store with a Rust runtime, such as the Redis Store or `MemoryStreamStore`, Core
reads, appends and closes a Stream itself, with no Activity. Workflow code reads with the same
`read`, `iter_read` and `iter_read_items` as a client. It gets the same pages, but each page has
at most 256 Records.

Here the Workflow reads the events of a model's answer while the model Activity still runs,
and starts each tool as its call arrives.

```python
events = workflow.get_stream(events_ref, body_type=ModelEvent)
status = workflow.get_stream(status_ref, body_type=str)
tools = []
async for record in events.iter_read_items(wait=timedelta(seconds=10)):
    event = record.body
    if event.kind == "tool_call":
        # The tool starts now, before the model ends its answer.
        tools.append(
            workflow.start_activity(
                run_tool, event.tool_call, start_to_close_timeout=timedelta(minutes=1)
            )
        )
        await status.append(f"Running {event.tool_call.name}")
results = await asyncio.gather(*tools)
```

The loop ends after the page that is `done`, when the Stream is closed and the read gave its
last Record. With no `wait`, a call gives what is in the Stream, and the loop ends at the first
empty page. With a `wait`, a call on a quiet Stream gives an empty page after the wait, and
the loop asks again. So a loop on a Stream that can be quiet for long uses `iter_read` and
leaves after an empty page. A later loop from the `last_offset` of that page goes on there.

```python
last = None
async for page in tokens.iter_read(from_offset=last, wait=timedelta(seconds=30)):
    for record in page.records:
        await shown.append(record.body.text)
    last = page.last_offset
    if not page.records:
        # The Stream was quiet for 30 s, or it is done.
        break
```

Core keeps one read open while a loop asks for its next page in the same activation. A loop
that awaits something else between two pages, as the `append` above does, lets the read close
before the activation completes, and its next page starts a new read. A read that no code
waits on never holds a Workflow Task open.

The calls of one Workflow Task write one marker in History, whatever the number of Records. A
read has three other costs.

- A call keeps its Workflow Task open while it waits for its page. A Signal, an Update, a
  cancel or an Activity result reaches the Workflow at the next Workflow Task, at the
  heartbeat of the task at the latest. That is about 8 s with the default task timeout of
  10 s. A call with a `wait` on a quiet Stream holds its task open for the whole wait.
- By default, `StreamReplaySource.STORE`, the marker holds a range of offsets, a count and a
  digest, and no Record. A replay reads the Store again, so the Records must stay in the
  Store for as long as the Workflow can replay. A replay whose Store stays unavailable for
  5 s fails only its Workflow Task, with `StreamReplayUnavailable`, and never the Workflow.
  The option
  `workflow_read_replay_from=StreamReplaySource.HISTORY` of `StreamConfig` puts the Records
  into the marker, so a replay needs no Store. Then one Workflow Task's marker holds at most
  1 MiB of Records. Past that, the task completes and the read goes on in the next one.
- A replay on a Worker with no cache reads the Store again on every Workflow Task.

While Workflow code is busy, a read holds at most 4 MiB of its Records in the Worker, then
waits until the code takes them. `workflow_read_ahead_bytes` of `StreamConfig` changes that
bound, for large Records or a Worker with little memory. A read gives Workflow code its Records
as soon as no activation of the Workflow is pending. A page that Workflow code gets in one
activation has at most 256 Records, and the rest come in the next activation with no wait.
`workflow_read_page_records` changes that cap, from 1 to 1000.

`workflow_read_min_gap` of `StreamConfig` sets the least time between two batches of one read.
It has no gap by default, and it must be at most 1 s. A read gives its first batch immediately.
The Records that come in the gap come together in the next batch. This trades latency for fewer
activations of Workflow code. A replay ignores both settings.

An append and a close of Workflow code always write as the Workflow, with the Workflow Task as
their epoch. A `producer=` of another kind raises `StreamError` with the reason
`STREAM_ERROR_REASON_INVALID`. An append runs inside its Workflow Task and records a marker with no body. An
append is at-least-once: when a Worker crashes or its run is evicted before the task with the
append completes, the rerun of that task writes a second copy with the epoch of the new task.

Core tries a read, an append or a close again while its Store answers `StorageUnavailable`.
It waits from 100 ms up to 2 s between tries, for at most 30 s. Only then does Workflow code
get the retryable `StreamError`. Every other reason raises on the first try. An append of
Workflow code always carries a `sequence`, so the Store writes its Records once, also when a
try reached the Store but its answer got lost.

A close spans two Workflow Tasks. The task that calls `close` records only the intent. Core
closes the Stream after the server took that task, and the next task gives the result. So a
failed task closes nothing, and its retry can still append. A Workflow that awaits its close
never completes in the task that asked for it. Await the close, also in a `finally` block.

```python
stream = await workflow.create_stream(id=stream_id, body_type=str)
try:
    await stream.append("a")
finally:
    await stream.close()
```

A close that the code doesn't await still happens when the Workflow ends in the same task,
after the server took that task. A graceful shutdown of the Worker waits for such a close, for
at most 10 s. Only a Worker that dies there, or a Store that doesn't answer in that time, loses
the close, and the Stream stays open.

A Store isn't meant to be written in Python, since such a Store has no Rust runtime. On one,
a read, an append and a close of Workflow code fail the Workflow.

## Metrics

Core records these metrics for the Direct calls of Workflow code. Their prefix is the metric
prefix of the `Runtime`, which is `temporal_` by default. Each metric has the attribute
`stream_store` with the Store of the call, next to the `namespace`, `task_queue` and
`workflow_type` that a Local Activity metric has. A replay that takes a result from a marker
counts nothing.

| Metric | What it counts |
| --- | --- |
| `temporal_stream_read_total` | Reads that Core started |
| `temporal_stream_read_failed` | Reads that ended with a failure |
| `temporal_stream_read_records` | Records that reads gave to Workflow code |
| `temporal_stream_append_total`, `_failed` | Appends that Core sent, and those that failed |
| `temporal_stream_append_latency` | From the start of an append in Core to its result |
| `temporal_stream_close_total`, `_failed` | Closes that Core sent, and those that failed |
| `temporal_stream_close_latency` | From the start of a close in Core to its result |
| `temporal_stream_close_abandoned` | Closes that a graceful shutdown gave up after 10 s |
| `temporal_stream_replay_read_total`, `_failed` | Marker entries that a replay read again |
| `temporal_stream_replay_read_latency` | The time of one such read and its check |
