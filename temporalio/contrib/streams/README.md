# Streams helpers

> This package is experimental. Its API can change in a later release.

`temporalio.contrib.streams` holds two helpers that sit on top of the Streams API of the SDK.
Each one takes a client `Stream` or a Workflow `Stream`, and works the same in client, Activity
and Workflow code.

## `BufferedAppender`

`BufferedAppender` batches the appends of one Stream. `append` returns at once, and the
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

## `pipeline`

`pipeline` chains steps over a Stream: `filter`, `map`, `take_until` and `limit`. A step runs
nothing. The end of the chain runs the whole chain as one `take`, `filter` or `reduce` call of
the Stream, so in Workflow code the chain is one Local Activity and one marker in History.

```python
from datetime import timedelta

from temporalio.contrib.streams import pipeline

text = await (
    pipeline(tokens)
    .filter(lambda record: record.body.kind == "text")
    .map(lambda record: record.body.text)
    .take_until(lambda text: text.endswith("\n"))
    .reduce(lambda joined, text: joined + text, "", timeout=timedelta(minutes=1))
)
```

An item is a `StreamRecord` until the first `map`, and the value of that map after it. The
functions of the steps must keep no state, since a Local Activity can run them again.

Each end takes `timeout`, `from_offset`, `idle_timeout`, `max_records` and `max_bytes`, as the
three calls do. Each result has `last_offset`, `stop` and `done`. `last_offset` is where the
scan stopped, so a loop that gives it as the next `from_offset` loses and repeats no Record.

| End | Call of the Stream | Gives back | Goes into History in Workflow code |
| --- | --- | --- | --- |
| `records(...)` | one `filter`, or one `take` when the chain keeps every Record | `StreamRead[BodyT]` with the kept Records | the Records |
| `reduce(function, initial, ...)` | one `reduce` | `StreamReduce[ValueT]` with the folded value | the value |
| `collect(...)` | one `reduce`, or as `records` when the chain has no `map` | `StreamReduce[list[T]]` with the items | the list |
| `count(...)` | one `reduce` | `StreamReduce[int]` with the number of items | the count |

`records` runs only a chain of `filter` steps, with at most one `take_until` and at most one
`limit` as its last step. For any other chain it raises `TypeError`, and `collect` or
`reduce` runs it. In Workflow code a result must fit in 1 MiB once encoded, since it goes
into History.
