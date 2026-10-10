Typed JSON payload conversion no longer truncates a float to an `int` when the type hint is a union of `int` and `float`, such as `int | float`.
