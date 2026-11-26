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
the Producer that an append of the running Activity attempt sends by default. An
Activity that stores it, such as in its heartbeat details, gets it back as a proto message, and
`as_producer` turns that into a `Producer` again, which `append(..., producer=...)` takes.

## Reads

A read of a Stream from `get_stream(StreamRef(id=...))` raises `TypeError`, since such a ref has
no body type. Pass `body_type=`, or a ref with a type argument such as `StreamRef[str](id=...)`.
A Stream from `create_stream` reads any body with no type.

A Stream has no end that a read reports, so a reader stops at the application's own last Record.
`read()` and `read_pages()` never end at a page with no Record, in client, Activity and Workflow
code. `read_pages()` yields that page, and both read again from its offset. The caller decides
when to stop, for example with a `break` or its own timeout. An iterator reads a page only when the
caller asks for the next Record, so a reader that stops at its last Record leaves no read open.

With no `wait`, the first read of an iterator gives the answer of the Store at once, and each later
read waits up to 1 minute for a new Record. So an iterator on an idle Stream doesn't call its Store
in a loop. A `wait` that the caller gives holds for each read. A `wait` of zero is no wait at all,
so such an iterator on an idle Stream reads again at once, page after page. `read_page()` has no
default wait.

In client and Activity code, the wait of a read ends shortly before its `rpc_timeout`, so that the
Store can answer in time. A read holds an Activity up to its wait, so an Activity that heartbeats
between Records needs a `wait` shorter than its heartbeat timeout, or a heartbeat from another
task.

In Workflow code, a read with no `wait` gives the answer of the Store, which can be a page with no
Record. A read with a `wait` and no Record to give waits up to its `wait`, but at most to the read
limit of its Workflow Task (the Worker option `stream_read_time`). At that limit `read_page` gives
a page with no Record. An iterator goes on until a Record comes, also over more than one Workflow
Task, and Workflow code can stop it with its own timer, a Signal or an Activity. Each page is a read
result in History. With a `wait` of zero or a short one, an iterator on a Stream with no new Record
gets page after page with no Record, and History grows fast. A read with a `wait` that has
no Record when its task completes starts again in the next task. It asks for a new Workflow Task
only when the Stream has more Records for it, so a read of an idle Stream makes no task. An outage
of the Store fails the Workflow Task, and Workflow code gets no error. A replay reads the Records
again from the Store, so the Retention of the Stream must cover the life of the Workflow and the
time in which it gets queries.

In Workflow code, a read with no `max_records` gives at most 256 Records in a page, and a read
with no `max_bytes` at most 4 MiB of them. A page has at most 16 MiB of Records, also for a larger
`max_bytes`. A read fails for a Record over the `max_record_bytes` of the Worker's `StreamConfig`,
so a reader needs a limit at least as large as that of each writer of its Stream.
