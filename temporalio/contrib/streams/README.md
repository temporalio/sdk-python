# Streams helpers

> This package is experimental. Its API can change in a later release.

`temporalio.contrib.streams` holds helpers that sit on top of the Streams API of the SDK. A
helper takes a client `Stream` or a Workflow `Stream`.

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
