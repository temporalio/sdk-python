Calling `Worker.run()` again on the same worker now raises `RuntimeError("Already started")` instead of a bridge panic.
