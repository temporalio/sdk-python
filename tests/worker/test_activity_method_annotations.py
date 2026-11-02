from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from typing_extensions import Self

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.worker import Worker


@dataclass
class Location:
    city: str


class LocationActivities:
    @activity.defn
    async def plain(self, loc: Location) -> str:
        return type(loc).__name__

    @activity.defn
    async def annotated(self: Self, loc: Location) -> str:
        return type(loc).__name__


@workflow.defn
class ActivityMethodAnnotationsWorkflow:
    @workflow.run
    async def run(self) -> list[str]:
        return [
            await workflow.execute_activity_method(
                LocationActivities.plain,
                Location("Rome"),
                start_to_close_timeout=timedelta(seconds=10),
            ),
            await workflow.execute_activity_method(
                LocationActivities.annotated,
                Location("Rome"),
                start_to_close_timeout=timedelta(seconds=10),
            ),
        ]


async def test_activity_method_with_annotated_self(client: Client):
    activities = LocationActivities()
    task_queue = str(uuid.uuid4())
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[ActivityMethodAnnotationsWorkflow],
        activities=[activities.plain, activities.annotated],
    ):
        plain, annotated = await client.execute_workflow(
            ActivityMethodAnnotationsWorkflow.run,
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )

    assert plain == "Location"
    assert annotated == "Location"
