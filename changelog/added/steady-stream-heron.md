Add experimental Streams for client, Activity and Workflow code, with configurable Stores and append limits. `MemoryStreamStore` supports tests and local runs.
Add Workflow Stream reads and replay through `Replayer(stream_config=...)`. Replay reads Records from the Store again, so retention must cover later replays and queries.
Add `BufferedAppender` for client and Activity batching, plus `current_producer` and `as_producer` for Activity attempts.
