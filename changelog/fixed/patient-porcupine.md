Exiting `async with Worker(...)` now waits for the Worker to finish its shutdown, including the `run_worker` cleanup of plugins, instead of cancelling the task that entered the block.
