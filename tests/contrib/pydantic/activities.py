from datetime import datetime
from typing import List
from uuid import UUID

from temporalio import activity
from tests.contrib.pydantic.models import PydanticModels


@activity.defn
async def pydantic_objects_activity(
    models: List[PydanticModels],
) -> List[PydanticModels]:
    return models


@activity.defn
async def misc_objects_activity(
    models: tuple[datetime, UUID],
) -> tuple[datetime, UUID]:
    return models
