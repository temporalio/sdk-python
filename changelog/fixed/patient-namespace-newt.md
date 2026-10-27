Fix `Worker.shutdown()` and `async with Worker(...)` hanging forever when worker validation fails, for example on a namespace that does not exist. `async with` now raises the validation error.
