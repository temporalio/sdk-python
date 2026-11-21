# Streams helpers

> This package is experimental. Its API can change in a later release.

`temporalio.contrib.streams` has helpers on top of the Streams API of the SDK. A Stream is an
append-only log of Records in a Stream Store, such as Redis. A client gets its Stores from
`stream_config=StreamConfig(stores=[...])`, and `create_stream` and `get_stream` of a client or
of `temporalio.workflow` return a `Stream`.

This isn't `temporalio.contrib.workflow_streams`, which keeps its events in a Workflow and
needs no Store.

## `BufferedAppender`

`BufferedAppender` batches the appends of one Stream. `append` returns immediately, and the
appender writes the buffered Records as one batch, at most `max_delay` after the first of them
(100 milliseconds by default). One write runs at a time, and the Records keep their order.

```python
from temporalio.contrib.streams import BufferedAppender

async with BufferedAppender(stream) as tokens:
    async for token in model_tokens():
        tokens.append(token)
```

The end of the `async with` block writes the Records that are still in the buffer. Without
`async with`, `await tokens.flush()` does the same. If code appends faster than the Store takes
Records, the buffer grows with no bound, unless that code awaits `flush()` from time to time. A
batch over `max_batch_records` or `max_batch_bytes` of the client's `StreamConfig` then fails,
and `max_batch_records` of the appender keeps each batch under the first.

An Activity that ends with Records in the buffer still writes them, up to `max_delay` later.
That write has the Producer of the ended attempt, it can come after a write of the next attempt,
and nobody gets its error. `async with`, or a `flush()` before the end, avoids it.

After a batch fails, the appender writes nothing more, and each later `append` and `flush`
raises the error of that batch, or `RuntimeError` if the write of the batch was cancelled.
`unwritten` is the number of Records that it left behind.

The appender is for client and Activity code. Workflow code passes its Records to one `append`
call of its Stream.

## A stored Producer

Each write has its Producer, which is the writer with its attempt. `current_producer()` gives
the Producer that an append or a close of the running Activity attempt sends by default. An
Activity that stores it, such as in its heartbeat details, gets it back as a proto message, and
`as_producer` turns that into a `Producer` again, which `append(..., producer=...)` takes.

## Reads

A read of a Stream from `get_stream(StreamRef(id=...))` raises `TypeError`, since such a ref has
no body type. Pass `body_type=`, or a ref with a type argument such as `StreamRef[str](id=...)`.
A Stream from `create_stream` reads any body with no type.

In Workflow code, a read with no `max_records` gives at most 256 Records in a page, and a read
with no `max_bytes` at most 4 MiB of them. A page has at most 16 MiB of Records, also for a larger
`max_bytes`. A read fails for a Record over the `max_record_bytes` of the Worker's `StreamConfig`,
so a reader needs a limit at least as large as that of each writer of its Stream.
