"""Guest-side loader for build-generated Python subroutine stubs."""

from collections.abc import Awaitable, Callable
from typing import cast

_bodies: dict[bytes, Callable[[int], Awaitable[int]]] = {}


async def invoke(slot: str, argument: int) -> int:
    """Execute the host-selected implementation in the active workflow."""
    from wit_world import resolve_addressable  # type: ignore[import-not-found]

    digest, source = resolve_addressable(slot)
    body = _bodies.get(digest)
    if body is None:
        name = f"<temporal-addressable:{digest.hex()}>"
        # Suspended calls retain the globals of their revision across activations.
        namespace: dict[str, object] = {"__name__": name}
        exec(compile(source, name, "exec", dont_inherit=True), namespace)
        body = cast(
            Callable[[int], Awaitable[int]], namespace["_temporal_addressable_entry"]
        )
        _bodies[digest] = body
    return await body(argument)
