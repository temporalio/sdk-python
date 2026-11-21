from datetime import timedelta
from functools import lru_cache

import temporalio.workflow as workflow

_labels: list[str] = []


def remember(value, /, *parts, prefix="v1", suffix="", **extra):
    _labels[:] = [prefix, "/".join([value, *parts]) + suffix + extra.get("mark", "")]


@lru_cache
def remembered() -> list[str]:
    return _labels.copy()


async def score(
    value: int, /, *, labels: list[str] | None = None
) -> tuple[int, list[str]]:
    await workflow.sleep(timedelta(seconds=5))
    return value + 10, labels or []
