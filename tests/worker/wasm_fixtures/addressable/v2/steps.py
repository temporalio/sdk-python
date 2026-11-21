from datetime import timedelta
from functools import lru_cache

import temporalio.workflow as workflow

_labels: list[str] = []


def remember(value, /, *parts, prefix="v2", suffix="", **extra):
    _labels[:] = [prefix, "/".join([value, *parts]) + suffix + extra.get("mark", "")]


@lru_cache
def remembered() -> list[str]:
    return _labels.copy()


async def score(
    value: int, /, *, labels: list[str] | None = None
) -> tuple[int, list[str]]:
    result = await workflow.execute_activity(
        "addressable_echo",
        value + 20,
        result_type=int,
        start_to_close_timeout=timedelta(seconds=10),
    )
    return result, labels or []
