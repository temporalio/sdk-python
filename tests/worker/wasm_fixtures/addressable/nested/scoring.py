from adjustment import adjust  # type: ignore[import-not-found]


async def score(value: int) -> tuple[int, object]:
    # The workflow compares the loaded callable across the helper rollout.
    return await _adjust_score(value), score


async def _adjust_score(value: int) -> int:
    return await adjust(value)
