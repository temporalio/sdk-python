# Stream helpers

> This package is experimental. Its API can change.

`temporalio.contrib.streams` supports the SDK's Stream API. Configure Stores with
`stream_config=StreamConfig(stores=[...])`. Use `create_stream` or `get_stream`
on the client or `temporalio.workflow` to get a `Stream`.

`temporalio.contrib.workflow_streams` is a separate API that stores events in
Workflow state.

## `BufferedAppender`

Use `BufferedAppender` in client or Activity code to combine frequent appends.

```python
from temporalio.contrib.streams import BufferedAppender

async with BufferedAppender(stream) as tokens:
    async for token in model_tokens():
        tokens.append(token)
```

`append` returns immediately. The appender starts a batch within `max_delay`
when no write is active. The default is 100 milliseconds. Writes are sequential.

`async with` flushes before exit. Without it, call `await tokens.flush()` before
the Activity ends. Otherwise a delayed write can follow a later Activity attempt,
and its error has no caller.

The buffer has no size limit. Await `flush()` periodically if the producer is
faster than the Store. Set the appender's `max_batch_records` to stay within the
client's batch count limit. The client's byte limits still apply.

A failed batch stops later writes. Later `append` and `flush` calls raise that
error, or `RuntimeError` if the write was cancelled. `unwritten` counts Records
in the failed batch and the remaining buffer. The Store may have accepted the
failed batch if only its response was lost.

Workflow code batches values in one Stream `append` call.

## Store a Producer

`current_producer()` returns the running Activity attempt's Producer. Use
`as_producer` to restore it from heartbeat details. Pass the restored value as
`append(..., producer=...)` when resumed work must retain that Producer.

## Read a Stream

Pass `body_type=` or a typed ref such as `StreamRef[str](id=...)` to `get_stream`.
A ref without a body type raises `TypeError` when read. A Stream from
`create_stream` without a body type accepts any body.

A Stream has no close. Stop at the application's terminal Record or cancel the
iterator. `read` and `read_pages` continue after empty pages. `read_pages` yields
those pages. `read_page` returns one page and has no default wait.

With no `wait`, an iterator requests its first page immediately. Later requests
wait up to one minute. An explicit `wait` applies to every page. Zero disables
waiting, so an idle Stream produces repeated empty pages. Client iterators yield
to the event loop between them so cancellation can run.

A positive `rpc_timeout` caps client and Activity waits below the timeout. An
Activity that heartbeats between Records needs a shorter `wait` than its
heartbeat timeout, or a separate heartbeat task.

Workflow reads can span Workflow Tasks. `stream_read_time` bounds how long a
read keeps its task open. Each page adds a result to History. Short waits on an
idle Stream can make History grow quickly. Store outages fail the Workflow Task
without returning an error to Workflow code.

Live Workflow pages default to 256 Records and 4 MiB, with a 16 MiB cap. A live
read fails if a Record exceeds the Worker's `max_record_bytes`. Replay preserves
recorded successes under smaller limits.

Replay reads Records from the Store again. Retention must cover every replay
and query. Values in Workflow state do not remove this Store dependency.
