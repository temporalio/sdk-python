from typing import List

from temporalio import activity
from tests.contrib.pydantic.models import PydanticModels


@activity.defn
async def pydantic_models_activity(
    models: List[PydanticModels],
) -> List[PydanticModels]:
    return models
