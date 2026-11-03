import uuid
from datetime import timedelta

import pytest

from temporalio import activity, workflow
from temporalio.api.worker.v1 import WorkerHeartbeat
from temporalio.api.workflowservice.v1 import (
    DescribeNamespaceRequest,
    ListWorkersRequest,
)
from temporalio.client import Client
from temporalio.runtime import Runtime, TelemetryConfig
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import PollerBehaviorSimpleMaximum
from tests import DEV_SERVER_DOWNLOAD_VERSION
from tests.helpers import assert_eventually, new_worker

AUTO_ENROLL_EXTRA_ARGS = [
    "--dynamic-config-value",
    "frontend.pollerAutoscalingAutoEnroll=true",
    "--dynamic-config-value",
    "frontend.WorkerHeartbeatsEnabled=true",
    "--dynamic-config-value",
    "frontend.ListWorkersEnabled=true",
]


@activity.defn
async def noop_activity() -> None:
    pass


@workflow.defn
class NoopWorkflow:
    @workflow.run
    async def run(self) -> None:
        await workflow.execute_activity(
            noop_activity, start_to_close_timeout=timedelta(seconds=10)
        )


async def _heartbeat(client: Client, task_queue: str) -> WorkerHeartbeat:
    resp = await client.workflow_service.list_workers(
        ListWorkersRequest(
            namespace=client.namespace,
            query=f'TaskQueue = "{task_queue}"',
            page_size=100,
        )
    )
    heartbeats = [
        info.worker_heartbeat
        for info in resp.workers_info
        if info.worker_heartbeat.task_queue == task_queue
    ]
    assert heartbeats, "no worker heartbeat yet"
    return heartbeats[0]


async def test_poller_autoscaling_auto_enroll(env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip("Time-skipping server does not implement ListWorkers")

    async with await WorkflowEnvironment.start_local(
        dev_server_extra_args=AUTO_ENROLL_EXTRA_ARGS,
        dev_server_download_version=DEV_SERVER_DOWNLOAD_VERSION,
    ) as local_env:
        ns = await local_env.client.workflow_service.describe_namespace(
            DescribeNamespaceRequest(namespace=local_env.client.namespace)
        )
        if not ns.namespace_info.capabilities.poller_autoscaling_auto_enroll:
            pytest.skip("Dev server does not support poller autoscaling auto-enroll")

        client = await local_env.connect_client(
            runtime=Runtime(
                telemetry=TelemetryConfig(),
                worker_heartbeat_interval=timedelta(seconds=1),
            )
        )

        # No poller config: the namespace auto-enrolls the worker into autoscaling.
        async with new_worker(
            client, NoopWorkflow, activities=[noop_activity]
        ) as worker:
            await client.execute_workflow(
                NoopWorkflow.run,
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

            async def assert_autoscaling() -> None:
                hb = await _heartbeat(client, worker.task_queue)
                assert hb.workflow_poller_info.is_autoscaling
                assert hb.activity_poller_info.is_autoscaling

            await assert_eventually(assert_autoscaling)

        # Explicit poller config opts out of auto-enroll.
        async with new_worker(
            client,
            NoopWorkflow,
            activities=[noop_activity],
            workflow_task_poller_behavior=PollerBehaviorSimpleMaximum(maximum=5),
            max_concurrent_activity_task_polls=5,
        ) as worker:
            await client.execute_workflow(
                NoopWorkflow.run,
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

            async def assert_fixed() -> None:
                hb = await _heartbeat(client, worker.task_queue)
                assert hb.workflow_poller_info.current_pollers > 0
                assert not hb.workflow_poller_info.is_autoscaling
                assert not hb.activity_poller_info.is_autoscaling

            await assert_eventually(assert_fixed)
