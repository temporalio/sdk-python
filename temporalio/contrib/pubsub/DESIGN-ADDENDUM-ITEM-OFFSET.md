# Per-Item Offsets — Addendum

Addendum to [DESIGN-ADDENDUM-TOPICS.md](./DESIGN-ADDENDUM-TOPICS.md). Revisits
the decision that `PubSubItem` does not carry an offset, based on experience
with the voice-terminal agent where the subscriber needs to track consumption
progress at item granularity.

## Problem

The voice-terminal agent streams TTS audio chunks through the pub/sub log.
Audio chunks are large (~50-100KB base64 each) and must not be truncated
from the workflow log until they have been **played** by the client, not merely
**received**.

The current API exposes offsets only at poll-batch granularity via
`PollResult.next_offset`. The subscriber cannot determine which global offset
corresponds to a specific item within the batch. This makes it impossible to
report fine-grained consumption progress back to the workflow for truncation.

### Why batch-level offsets are insufficient

The subscriber's consumption model has two stages:

1. **Receive**: items are yielded by `subscribe()` and buffered locally
   (e.g., audio enqueued into a playback buffer).
2. **Consume**: the local consumer finishes processing the item (e.g., the
   speaker finishes playing the audio).

The subscriber needs to signal the workflow: "I have consumed through offset N,
you may truncate up to N." This requires knowing the offset of each item, not
just the offset at the end of a poll batch.

Without per-item offsets, the subscriber can only report the batch boundary.
If the subscriber crashes after receiving a batch but before consuming all
items, truncation based on the batch boundary discards unconsumed items.

### Why this matters for continue-as-new

Before continue-as-new, the workflow must serialize the pub/sub log into the
workflow input. Audio chunks make the log large (observed 3.6MB, exceeding
Temporal's payload size limit). The workflow needs to truncate consumed items
before serialization, but can only safely truncate items the subscriber has
actually consumed — which requires per-item offset tracking.

### Workaround: count items from `from_offset`

When the subscriber requests all topics (no filtering), items map 1:1 to
consecutive global offsets. The subscriber can compute `from_offset + i` for
each item. This works for the voice-terminal (which subscribes to all topics)
but is fragile — it breaks silently if topic filtering is introduced or if a
third topic is added to the workflow without updating the subscription.

## Proposed Change

Add an `offset` field to `PubSubItem` and `_WireItem`, populated by the poll
handler from the item's position in the log. No new storage in the workflow —
the offset is computed at poll time.

### Wire types (revised)

```python
@dataclass
class PubSubItem:
    topic: str
    data: bytes
    offset: int = 0

@dataclass
class _WireItem:
    topic: str
    data: str  # base64-encoded bytes
    offset: int = 0
```

### Poll handler change

The poll handler already iterates the log slice. It annotates each item with
its global offset before returning:

```python
all_new = self._pubsub_log[log_offset:]
next_offset = self._pubsub_base_offset + len(self._pubsub_log)
if input.topics:
    topic_set = set(input.topics)
    filtered = [
        (self._pubsub_base_offset + log_offset + i, item)
        for i, item in enumerate(all_new)
        if item.topic in topic_set
    ]
else:
    filtered = [
        (self._pubsub_base_offset + log_offset + i, item)
        for i, item in enumerate(all_new)
    ]
return PollResult(
    items=[
        _WireItem(topic=item.topic, data=encode_data(item.data), offset=off)
        for off, item in filtered
    ],
    next_offset=next_offset,
)
```

### `subscribe()` change

The client passes the offset through to the yielded `PubSubItem`:

```python
for wire_item in result.items:
    yield PubSubItem(
        topic=wire_item.topic,
        data=decode_data(wire_item.data),
        offset=wire_item.offset,
    )
```

### Backward compatibility

The `offset` field defaults to `0` on both `PubSubItem` and `_WireItem`.
Existing subscribers that don't use the field are unaffected. Workflows
running old code that don't populate the field will return `0` for all items —
subscribers must treat `offset=0` as "unknown" if they depend on it.

## Subscriber consumption tracking pattern

With per-item offsets, the voice-terminal client can track played-through
progress:

```python
played_offset = from_offset

async for item in pubsub.subscribe(from_offset=from_offset):
    if item.topic == AUDIO_TOPIC:
        player.enqueue(pcm, offset=item.offset)
    elif item.topic == EVENTS_TOPIC:
        # Events are consumed immediately on receipt
        played_offset = item.offset + 1
        if event_type == "TURN_COMPLETE":
            break

# After playback finishes, update played_offset from the player
played_offset = player.last_played_offset

# Signal the workflow to truncate consumed items
await handle.signal(workflow.truncate, played_offset)
```

The workflow truncates only up to `played_offset`, preserving any items the
subscriber has received but not yet consumed. Before continue-as-new, the
workflow truncates to the last acked offset rather than the log tail.

## Properties

- **No new workflow state.** Offsets are computed at poll time from
  `base_offset` and the item's position in the log.
- **Backward compatible.** Default `offset=0` means existing code is
  unaffected.
- **Enables safe truncation.** Subscribers can report exactly which items
  they have consumed, not just which batches they have received.
- **Works with topic filtering.** Per-item offsets are correct regardless of
  which topics the subscriber requests.

## Relationship to existing design

The [DESIGN-ADDENDUM-TOPICS.md](./DESIGN-ADDENDUM-TOPICS.md) states:

> `PubSubItem` does not carry an offset. The global offset is an internal
> detail exposed only through `PollResult.next_offset` and the `get_offset()`
> query.

This addendum revises that decision. The global offset is no longer purely
internal — it is exposed per-item to enable consumption tracking. The offset
model (global, monotonic, single log) is unchanged. The BFF containment
strategy for end-client leakage is also unchanged — the BFF still assigns its
own SSE event IDs.
