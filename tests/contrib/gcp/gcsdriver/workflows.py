"""Workflow definitions kept separate from optional GCS client imports."""

from datetime import timedelta

import temporalio.activity
import temporalio.workflow


@temporalio.activity.defn
async def echo_activity(value: str) -> str:
    return value


@temporalio.workflow.defn
class EchoWorkflow:
    @temporalio.workflow.run
    async def run(self, value: str) -> str:
        return await temporalio.workflow.execute_activity(
            echo_activity, value, start_to_close_timeout=timedelta(seconds=10)
        )
