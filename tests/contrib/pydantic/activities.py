from datetime import datetime
from uuid import UUID

from temporalio import activity
from tests.contrib.pydantic.models import PydanticModels


@activity.defn
async def pydantic_objects_activity(
    models: list[PydanticModels],
) -> list[PydanticModels]:
    return models


@activity.defn
async def misc_objects_activity(
    models: tuple[
        int,
        str,
        dict[str, float],
        list[dict[str, float]],
        tuple[dict[str, float]],
        datetime,
        UUID,
    ],
) -> tuple[
    int,
    str,
    dict[str, float],
    list[dict[str, float]],
    tuple[dict[str, float]],
    datetime,
    UUID,
]:
    return models
