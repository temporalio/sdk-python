# Streams helpers

> This package is experimental, and its API can change.

`temporalio.contrib.streams` holds helpers that sit on top of the Streams API of the SDK. They
run in a client or an Activity, and take a client `Stream`.

## `BufferedAppender`

`BufferedAppender` batches the appends of one Stream. `append` returns immediately, and the
appender writes the buffered Records as one batch. A batch waits at most `max_delay`.

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
