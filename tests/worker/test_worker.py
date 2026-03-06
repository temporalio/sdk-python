from __future__ import annotations

import asyncio
import concurrent.futures
import multiprocessing
import multiprocessing.context
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from typing import Any
from urllib.request import urlopen

import nexusrpc
import pytest

import temporalio.api.enums.v1
import temporalio.nexus
import temporalio.worker._worker
from temporalio import activity, workflow
from temporalio.api.workflowservice.v1 import (
    DescribeWorkerDeploymentRequest,
    DescribeWorkerDeploymentResponse,
    SetWorkerDeploymentCurrentVersionRequest,
    SetWorkerDeploymentCurrentVersionResponse,
    SetWorkerDeploymentRampingVersionRequest,
    SetWorkerDeploymentRampingVersionResponse,
)
from temporalio.client import (
    Client,
)
from temporalio.common import PinnedVersioningOverride, RawValue, VersioningBehavior
from temporalio.runtime import (
    PrometheusConfig,
    Runtime,
    TelemetryConfig,
)
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import (
    ActivitySlotInfo,
    CustomSlotSupplier,
    FixedSizeSlotSupplier,
    LocalActivitySlotInfo,
    NexusSlotInfo,
    PollerBehaviorAutoscaling,
    ResourceBasedSlotConfig,
    ResourceBasedSlotSupplier,
    ResourceBasedTunerConfig,
    SlotMarkUsedContext,
    SlotPermit,
    SlotReleaseContext,
    SlotReserveContext,
    Worker,
    WorkerDeploymentConfig,
    WorkerDeploymentVersion,
    WorkerTuner,
    WorkflowSlotInfo,
)
from temporalio.workflow import DynamicWorkflowConfig, VersioningIntent
from tests.helpers import (
    assert_eventually,
    find_free_port,
    new_worker,
)
from tests.helpers.fork import _ForkTestResult, _TestFork
from tests.helpers.nexus import make_nexus_endpoint_name


def test_load_default_worker_binary_id():
    # Just run it twice and confirm it didn't change
    val1 = temporalio.worker._worker.load_default_build_id(memoize=False)
    val2 = temporalio.worker._worker.load_default_build_id(memoize=False)
    assert val1 == val2


@activity.defn
async def never_run_activity() -> None:
    raise NotImplementedError


@workflow.defn
class NeverRunWorkflow:
    @workflow.run
    async def run(self) -> None:
        raise NotImplementedError


@nexusrpc.handler.service_handler
class NeverRunService:
    @nexusrpc.handler.sync_operation
    async def never_run_operation(
        self, _ctx: nexusrpc.handler.StartOperationContext, _input: None
    ) -> None:
        raise NotImplementedError

    @temporalio.nexus.workflow_run_operation
    async def never_run_workflow_run_operation(
        self, _ctx: temporalio.nexus.WorkflowRunOperationContext, _input: None
    ) -> temporalio.nexus.WorkflowHandle[None]:
        raise NotImplementedError


async def test_worker_fatal_error_run(client: Client):
    # Run worker with injected workflow poll error
    worker = create_worker(client)
    with pytest.raises(RuntimeError) as err:
        with WorkerFailureInjector(worker) as inj:
            inj.workflow.poll_fail_queue.put_nowait(RuntimeError("OH NO"))
            await worker.run()
    assert str(err.value) == "Workflow worker failed"
    assert err.value.__cause__ and str(err.value.__cause__) == "OH NO"

    # Run worker with injected activity poll error
    worker = create_worker(client)
    with pytest.raises(RuntimeError) as err:
        with WorkerFailureInjector(worker) as inj:
            inj.activity.poll_fail_queue.put_nowait(RuntimeError("OH NO"))
            await worker.run()
    assert str(err.value) == "Activity worker failed"
    assert err.value.__cause__ and str(err.value.__cause__) == "OH NO"

    # Run worker with them both injected (was causing warning for not retrieving
    # the second error)
    worker = create_worker(client)
    with pytest.raises(RuntimeError) as err:
        with WorkerFailureInjector(worker) as inj:
            inj.workflow.poll_fail_queue.put_nowait(RuntimeError("OH NO"))
            inj.activity.poll_fail_queue.put_nowait(RuntimeError("OH NO"))
            await worker.run()
    assert str(err.value).endswith("worker failed")
    assert err.value.__cause__ and str(err.value.__cause__) == "OH NO"


async def test_worker_fatal_error_with(client: Client):
    # Start the worker, wait a short bit, fail it, wait for long time (will be
    # cancelled)
    worker = create_worker(client)
    with pytest.raises(RuntimeError) as err:
        with WorkerFailureInjector(worker) as inj:
            async with worker:
                await asyncio.sleep(0.1)
                inj.workflow.poll_fail_queue.put_nowait(RuntimeError("OH NO"))
                await asyncio.sleep(1000)
    assert str(err.value) == "Workflow worker failed"
    assert err.value.__cause__ and str(err.value.__cause__) == "OH NO"

    # Raise inside the async with and confirm it works
    worker = create_worker(client)
    with pytest.raises(RuntimeError) as err:
        with WorkerFailureInjector(worker) as inj:
            async with worker:
                raise RuntimeError("IN WITH")
    assert str(err.value) == "IN WITH"

    # Demonstrate that inner re-thrown failure swallows worker failure
    worker = create_worker(client)
    with pytest.raises(RuntimeError) as err:
        with WorkerFailureInjector(worker) as inj:
            async with worker:
                inj.workflow.poll_fail_queue.put_nowait(RuntimeError("OH NO"))
                try:
                    await asyncio.sleep(1000)
                except BaseException as inner_err:
                    raise RuntimeError("Caught cancel") from inner_err
    assert str(err.value) == "Caught cancel"
    assert err.value.__cause__ and type(err.value.__cause__) is asyncio.CancelledError


async def test_worker_fatal_error_callback(client: Client):
    callback_err: BaseException | None = None

    async def on_fatal_error(exc: BaseException) -> None:
        nonlocal callback_err
        callback_err = exc

    worker = create_worker(client, on_fatal_error)
    with pytest.raises(RuntimeError) as err:
        with WorkerFailureInjector(worker) as inj:
            async with worker:
                await asyncio.sleep(0.1)
                inj.workflow.poll_fail_queue.put_nowait(RuntimeError("OH NO"))
                await asyncio.sleep(1000)
    assert err.value is callback_err


async def test_worker_cancel_run(client: Client):
    worker = create_worker(client)
    assert not worker.is_running and not worker.is_shutdown
    run_task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.3)
    assert worker.is_running and not worker.is_shutdown
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    assert not worker.is_running and worker.is_shutdown


@activity.defn
async def say_hello(name: str) -> str:
    return f"Hello, {name}!"


@workflow.defn
class WaitOnSignalWorkflow:
    def __init__(self) -> None:
        self._last_signal = "<none>"

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self._last_signal == "finish")
        await workflow.execute_activity(
            say_hello,
            "hi",
            versioning_intent=VersioningIntent.DEFAULT,
            start_to_close_timeout=timedelta(seconds=5),
        )

    @workflow.signal
    def my_signal(self, value: str) -> None:
        self._last_signal = value
        workflow.logger.info(f"Signal: {value}")


async def test_worker_validate_fail(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip("Java test server does not appear to fail on invalid namespace")
    # Try to run a worker on an invalid namespace
    config = client.config()
    config["namespace"] = "does-not-exist"
    client = Client(**config)
    with pytest.raises(RuntimeError) as err:
        await Worker(
            client, task_queue=f"tq-{uuid.uuid4()}", workflows=[NeverRunWorkflow]
        ).run()
    assert str(err.value).startswith("Worker validation failed")


async def test_can_run_resource_based_worker(client: Client):
    tuner = WorkerTuner.create_resource_based(
        target_memory_usage=0.5,
        target_cpu_usage=0.5,
        workflow_config=ResourceBasedSlotConfig(5, 20, timedelta(seconds=0)),
        # Ensure we can assume defaults when specifying only some options
        activity_config=ResourceBasedSlotConfig(minimum_slots=1),
    )
    async with new_worker(
        client,
        WaitOnSignalWorkflow,
        activities=[say_hello],
        tuner=tuner,
    ) as w:
        wf1 = await client.start_workflow(
            WaitOnSignalWorkflow.run,
            id=f"resource-based-{uuid.uuid4()}",
            task_queue=w.task_queue,
        )
        await wf1.signal(WaitOnSignalWorkflow.my_signal, "finish")
        await wf1.result()


async def test_can_run_composite_tuner_worker(client: Client):
    resource_based_options = ResourceBasedTunerConfig(0.5, 0.5)
    tuner = WorkerTuner.create_composite(
        workflow_supplier=FixedSizeSlotSupplier(5),
        activity_supplier=ResourceBasedSlotSupplier(
            ResourceBasedSlotConfig(
                minimum_slots=1,
                maximum_slots=20,
                ramp_throttle=timedelta(milliseconds=60),
            ),
            resource_based_options,
        ),
        local_activity_supplier=ResourceBasedSlotSupplier(
            ResourceBasedSlotConfig(
                minimum_slots=1,
                maximum_slots=5,
                ramp_throttle=timedelta(milliseconds=60),
            ),
            resource_based_options,
        ),
        nexus_supplier=FixedSizeSlotSupplier(10),
    )
    async with new_worker(
        client,
        WaitOnSignalWorkflow,
        activities=[say_hello],
        tuner=tuner,
    ) as w:
        wf1 = await client.start_workflow(
            WaitOnSignalWorkflow.run,
            id=f"composite-tuner-{uuid.uuid4()}",
            task_queue=w.task_queue,
        )
        await wf1.signal(WaitOnSignalWorkflow.my_signal, "finish")
        await wf1.result()


async def test_cant_specify_max_concurrent_and_tuner(client: Client):
    tuner = WorkerTuner.create_resource_based(
        target_memory_usage=0.5,
        target_cpu_usage=0.5,
        workflow_config=ResourceBasedSlotConfig(5, 20, timedelta(seconds=0)),
    )
    with pytest.raises(ValueError) as err:
        async with new_worker(
            client,
            WaitOnSignalWorkflow,
            activities=[say_hello],
            tuner=tuner,
            max_concurrent_workflow_tasks=10,
        ):
            pass
    assert "Cannot specify " in str(err.value)
    assert "when also specifying tuner" in str(err.value)


async def test_warns_when_workers_too_low(client: Client):
    tuner = WorkerTuner.create_resource_based(
        target_memory_usage=0.5,
        target_cpu_usage=0.5,
    )
    with concurrent.futures.ThreadPoolExecutor() as executor:
        with pytest.warns(
            UserWarning,
            match="Worker max_concurrent_activities is 500 but activity_executor's max_workers is only",
        ):
            async with new_worker(
                client,
                WaitOnSignalWorkflow,
                activities=[say_hello],
                tuner=tuner,
                activity_executor=executor,
            ):
                pass
    with concurrent.futures.ThreadPoolExecutor() as executor:
        with pytest.warns(
            UserWarning,
            match="Worker max_concurrent_nexus_tasks is 500 but nexus_task_executor's max_workers is only",
        ):
            async with new_worker(
                client,
                WaitOnSignalWorkflow,
                nexus_service_handlers=[NeverRunService()],
                tuner=tuner,
                nexus_task_executor=executor,
            ):
                pass


@nexusrpc.handler.service_handler
class SayHelloService:
    @nexusrpc.handler.sync_operation
    async def say_hello(
        self, _ctx: nexusrpc.handler.StartOperationContext, name: str
    ) -> str:
        return f"Hello, {name}!"


@workflow.defn
class CustomSlotSupplierWorkflow:
    def __init__(self) -> None:
        self._last_signal = "<none>"

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self._last_signal == "finish")
        await workflow.execute_activity(
            say_hello,
            "hi",
            versioning_intent=VersioningIntent.DEFAULT,
            start_to_close_timeout=timedelta(seconds=5),
        )
        nexus_client = workflow.create_nexus_client(
            endpoint=make_nexus_endpoint_name(workflow.info().task_queue),
            service=SayHelloService,
        )
        await nexus_client.execute_operation(
            SayHelloService.say_hello,
            "hi",
        )

    @workflow.signal
    def my_signal(self, value: str) -> None:
        self._last_signal = value
        workflow.logger.info(f"Signal: {value}")


async def test_custom_slot_supplier(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work under Java test server")

    class MyPermit(SlotPermit):
        def __init__(self, pnum: int):
            super().__init__()
            self.pnum = pnum

    class MySlotSupplier(CustomSlotSupplier):
        reserves = 0
        releases = 0
        highest_seen_reserve_on_release = 0
        used = 0
        seen_sticky_kinds = set()
        seen_slot_kinds = set()
        seen_used_slot_kinds = set()
        seen_release_info_empty = False
        seen_release_info_nonempty = False

        async def reserve_slot(self, ctx: SlotReserveContext) -> SlotPermit:
            self.reserve_asserts(ctx)
            # Verify an async call doesn't bungle things
            await asyncio.sleep(0.01)
            self.reserves += 1
            return MyPermit(self.reserves)

        def try_reserve_slot(self, ctx: SlotReserveContext) -> SlotPermit | None:
            self.reserve_asserts(ctx)
            return None

        def mark_slot_used(self, ctx: SlotMarkUsedContext) -> None:
            assert ctx.permit is not None
            assert isinstance(ctx.permit, MyPermit)
            assert ctx.permit.pnum is not None
            assert ctx.slot_info is not None
            if isinstance(ctx.slot_info, WorkflowSlotInfo):
                self.seen_used_slot_kinds.add("wf")
            elif isinstance(ctx.slot_info, ActivitySlotInfo):
                self.seen_used_slot_kinds.add("a")
            elif isinstance(ctx.slot_info, LocalActivitySlotInfo):
                self.seen_used_slot_kinds.add("la")
            elif isinstance(ctx.slot_info, NexusSlotInfo):
                self.seen_used_slot_kinds.add("nx")
            self.used += 1

        def release_slot(self, ctx: SlotReleaseContext) -> None:
            assert ctx.permit is not None
            assert isinstance(ctx.permit, MyPermit)
            assert ctx.permit.pnum is not None
            self.highest_seen_reserve_on_release = max(
                ctx.permit.pnum, self.highest_seen_reserve_on_release
            )
            # Info may be empty, and we should see both empty and not
            if ctx.slot_info is None:
                self.seen_release_info_empty = True
            else:
                self.seen_release_info_nonempty = True
            self.releases += 1

        def reserve_asserts(self, ctx: SlotReserveContext) -> None:
            assert ctx.task_queue is not None
            assert ctx.worker_identity is not None
            assert ctx.worker_build_id is not None
            self.seen_sticky_kinds.add(ctx.is_sticky)
            self.seen_slot_kinds.add(ctx.slot_type)

    ss = MySlotSupplier()

    tuner = WorkerTuner.create_composite(
        workflow_supplier=ss,
        activity_supplier=ss,
        local_activity_supplier=ss,
        nexus_supplier=ss,
    )
    async with new_worker(
        client,
        CustomSlotSupplierWorkflow,
        activities=[say_hello],
        nexus_service_handlers=[SayHelloService()],
        tuner=tuner,
        identity="myworker",
    ) as w:
        endpoint_name = make_nexus_endpoint_name(w.task_queue)
        await env.create_nexus_endpoint(endpoint_name, w.task_queue)
        wf1 = await client.start_workflow(
            CustomSlotSupplierWorkflow.run,
            id=f"custom-slot-supplier-{uuid.uuid4()}",
            task_queue=w.task_queue,
        )
        await wf1.signal(CustomSlotSupplierWorkflow.my_signal, "finish")
        await wf1.result()

    # We can't use reserve number directly because there is a technically possible race
    # where the python reserve function appears to complete, but Rust doesn't see that.
    # This isn't solvable without redoing a chunk of pyo3-asyncio. So we only check
    # that the permits passed to release line up.
    assert ss.highest_seen_reserve_on_release >= ss.releases
    assert ss.used == 5
    assert ss.seen_sticky_kinds == {True, False}
    assert ss.seen_slot_kinds == {"workflow", "activity", "local-activity", "nexus"}
    assert ss.seen_used_slot_kinds == {"wf", "a", "nx"}
    assert ss.seen_release_info_empty
    assert ss.seen_release_info_nonempty


@workflow.defn
class SimpleWorkflow:
    @workflow.run
    async def run(self) -> str:
        return "hi"


async def test_throwing_slot_supplier(client: Client):
    """Ensures a (mostly) broken slot supplier doesn't hose everything up"""

    class ThrowingSlotSupplier(CustomSlotSupplier):
        marked_used = False

        async def reserve_slot(self, ctx: SlotReserveContext) -> SlotPermit:
            # Hand out workflow tasks until one is used
            if ctx.slot_type == "workflow" and not self.marked_used:
                return SlotPermit()
            raise ValueError("I always throw")

        def try_reserve_slot(self, ctx: SlotReserveContext) -> SlotPermit | None:
            raise ValueError("I always throw")

        def mark_slot_used(self, ctx: SlotMarkUsedContext) -> None:
            raise ValueError("I always throw")

        def release_slot(self, ctx: SlotReleaseContext) -> None:
            raise ValueError("I always throw")

    ss = ThrowingSlotSupplier()

    tuner = WorkerTuner.create_composite(
        workflow_supplier=ss,
        activity_supplier=ss,
        local_activity_supplier=ss,
        nexus_supplier=ss,
    )
    async with new_worker(
        client,
        SimpleWorkflow,
        activities=[say_hello],
        tuner=tuner,
    ) as w:
        wf1 = await client.start_workflow(
            SimpleWorkflow.run,
            id=f"throwing-slot-supplier-{uuid.uuid4()}",
            task_queue=w.task_queue,
        )
        await wf1.result()


async def test_blocking_slot_supplier(client: Client):
    class BlockingSlotSupplier(CustomSlotSupplier):
        marked_used = False

        async def reserve_slot(self, ctx: SlotReserveContext) -> SlotPermit:
            await asyncio.get_event_loop().create_future()
            raise ValueError("Should be unreachable")

        def try_reserve_slot(self, ctx: SlotReserveContext) -> SlotPermit | None:
            return None

        def mark_slot_used(self, ctx: SlotMarkUsedContext) -> None:
            return None

        def release_slot(self, ctx: SlotReleaseContext) -> None:
            return None

    ss = BlockingSlotSupplier()

    tuner = WorkerTuner.create_composite(
        workflow_supplier=ss,
        activity_supplier=ss,
        local_activity_supplier=ss,
        nexus_supplier=ss,
    )
    async with new_worker(
        client,
        SimpleWorkflow,
        activities=[say_hello],
        tuner=tuner,
    ) as _w:
        await asyncio.sleep(1)


@workflow.defn(
    name="DeploymentVersioningWorkflow",
    versioning_behavior=VersioningBehavior.AUTO_UPGRADE,
)
class DeploymentVersioningWorkflowV1AutoUpgrade:
    def __init__(self) -> None:
        self.finish = False

    @workflow.run
    async def run(self):
        await workflow.wait_condition(lambda: self.finish)
        return "version-v1"

    @workflow.signal
    def do_finish(self):
        self.finish = True

    @workflow.query
    def state(self):
        return "v1"


@workflow.defn(
    name="DeploymentVersioningWorkflow", versioning_behavior=VersioningBehavior.PINNED
)
class DeploymentVersioningWorkflowV2Pinned:
    def __init__(self) -> None:
        self.finish = False

    @workflow.run
    async def run(self):
        await workflow.wait_condition(lambda: self.finish)
        depver = workflow.info().get_current_deployment_version()
        assert depver
        assert depver.build_id == "2.0"
        # Just ensuring the rust object was converted properly and this method still works
        workflow.logger.debug(f"Dep string: {depver.to_canonical_string()}")
        return "version-v2"

    @workflow.signal
    def do_finish(self):
        self.finish = True

    @workflow.query
    def state(self):
        return "v2"


@workflow.defn(
    name="DeploymentVersioningWorkflow",
    versioning_behavior=VersioningBehavior.AUTO_UPGRADE,
)
class DeploymentVersioningWorkflowV3AutoUpgrade:
    def __init__(self) -> None:
        self.finish = False

    @workflow.run
    async def run(self):
        await workflow.wait_condition(lambda: self.finish)
        return "version-v3"

    @workflow.signal
    def do_finish(self):
        self.finish = True

    @workflow.query
    def state(self):
        return "v3"


async def test_worker_with_worker_deployment_config(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("Test Server doesn't support worker deployments")

    deployment_name = f"deployment-{uuid.uuid4()}"
    worker_v1 = WorkerDeploymentVersion(deployment_name=deployment_name, build_id="1.0")
    worker_v2 = WorkerDeploymentVersion(deployment_name=deployment_name, build_id="2.0")
    worker_v3 = WorkerDeploymentVersion(deployment_name=deployment_name, build_id="3.0")
    async with (
        new_worker(
            client,
            DeploymentVersioningWorkflowV1AutoUpgrade,
            deployment_config=WorkerDeploymentConfig(
                version=worker_v1,
                use_worker_versioning=True,
            ),
        ) as w1,
        new_worker(
            client,
            DeploymentVersioningWorkflowV2Pinned,
            deployment_config=WorkerDeploymentConfig(
                version=worker_v2,
                use_worker_versioning=True,
            ),
            task_queue=w1.task_queue,
        ),
        new_worker(
            client,
            DeploymentVersioningWorkflowV3AutoUpgrade,
            deployment_config=WorkerDeploymentConfig(
                version=worker_v3,
                use_worker_versioning=True,
            ),
            task_queue=w1.task_queue,
        ),
    ):
        describe_resp = await wait_until_worker_deployment_visible(
            client,
            worker_v1,
        )
        await set_current_deployment_version(
            client, describe_resp.conflict_token, worker_v1
        )

        # Start workflow 1 which will use the 1.0 worker on auto-upgrade
        wf1 = await client.start_workflow(
            DeploymentVersioningWorkflowV1AutoUpgrade.run,
            id="basic-versioning-v1",
            task_queue=w1.task_queue,
        )
        assert "v1" == await wf1.query("state")

        describe_resp2 = await wait_until_worker_deployment_visible(client, worker_v2)
        await set_current_deployment_version(
            client, describe_resp2.conflict_token, worker_v2
        )

        wf2 = await client.start_workflow(
            DeploymentVersioningWorkflowV2Pinned.run,
            id="basic-versioning-v2",
            task_queue=w1.task_queue,
        )
        assert "v2" == await wf2.query("state")

        describe_resp3 = await wait_until_worker_deployment_visible(client, worker_v3)
        await set_current_deployment_version(
            client, describe_resp3.conflict_token, worker_v3
        )

        wf3 = await client.start_workflow(
            DeploymentVersioningWorkflowV3AutoUpgrade.run,
            id="basic-versioning-v3",
            task_queue=w1.task_queue,
        )
        assert "v3" == await wf3.query("state")

        # Signal all workflows to finish
        await wf1.signal(DeploymentVersioningWorkflowV1AutoUpgrade.do_finish)
        await wf2.signal(DeploymentVersioningWorkflowV2Pinned.do_finish)
        await wf3.signal(DeploymentVersioningWorkflowV3AutoUpgrade.do_finish)

        res1 = await wf1.result()
        res2 = await wf2.result()
        res3 = await wf3.result()

        assert res1 == "version-v3"
        assert res2 == "version-v2"
        assert res3 == "version-v3"


async def test_worker_deployment_ramp(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip("Test Server doesn't support worker deployments")

    deployment_name = f"deployment-ramping-{uuid.uuid4()}"
    v1 = WorkerDeploymentVersion(deployment_name=deployment_name, build_id="1.0")
    v2 = WorkerDeploymentVersion(deployment_name=deployment_name, build_id="2.0")
    async with (
        new_worker(
            client,
            DeploymentVersioningWorkflowV1AutoUpgrade,
            deployment_config=WorkerDeploymentConfig(
                version=v1, use_worker_versioning=True
            ),
        ) as w1,
        new_worker(
            client,
            DeploymentVersioningWorkflowV2Pinned,
            deployment_config=WorkerDeploymentConfig(
                version=v2, use_worker_versioning=True
            ),
            task_queue=w1.task_queue,
        ),
    ):
        await wait_until_worker_deployment_visible(client, v1)
        describe_resp = await wait_until_worker_deployment_visible(client, v2)

        # Set current version to v1 and ramp v2 to 100%
        conflict_token = (
            await set_current_deployment_version(
                client, describe_resp.conflict_token, v1
            )
        ).conflict_token
        conflict_token = (
            await set_ramping_version(client, conflict_token, v2, 100)
        ).conflict_token

        # Run workflows and verify they run on v2
        for i in range(3):
            wf = await client.start_workflow(
                DeploymentVersioningWorkflowV2Pinned.run,
                id=f"versioning-ramp-100-{i}-{uuid.uuid4()}",
                task_queue=w1.task_queue,
            )
            await wf.signal(DeploymentVersioningWorkflowV2Pinned.do_finish)
            res = await wf.result()
            assert res == "version-v2"

        # Set ramp to 0, expecting workflows to run on v1
        conflict_token = (
            await set_ramping_version(client, conflict_token, v2, 0)
        ).conflict_token
        for i in range(3):
            wfa = await client.start_workflow(
                DeploymentVersioningWorkflowV1AutoUpgrade.run,
                id=f"versioning-ramp-0-{i}-{uuid.uuid4()}",
                task_queue=w1.task_queue,
            )
            await wfa.signal(DeploymentVersioningWorkflowV1AutoUpgrade.do_finish)
            res = await wfa.result()
            assert res == "version-v1"

        # Set ramp to 50 and eventually verify workflows run on both versions
        await set_ramping_version(client, conflict_token, v2, 50)
        seen_results = set()

        async def run_and_record():
            wf = await client.start_workflow(
                DeploymentVersioningWorkflowV1AutoUpgrade.run,
                id=f"versioning-ramp-50-{uuid.uuid4()}",
                task_queue=w1.task_queue,
            )
            await wf.signal(DeploymentVersioningWorkflowV1AutoUpgrade.do_finish)
            return await wf.result()

        async def check_results():
            res = await run_and_record()
            seen_results.add(res)
            assert "version-v1" in seen_results and "version-v2" in seen_results

        await assert_eventually(check_results)


@workflow.defn(dynamic=True, versioning_behavior=VersioningBehavior.PINNED)
class DynamicWorkflowVersioningOnDefn:
    @workflow.run
    async def run(self, _args: Sequence[RawValue]) -> str:
        return "dynamic"


@workflow.defn(dynamic=True, versioning_behavior=VersioningBehavior.PINNED)
class DynamicWorkflowVersioningOnConfigMethod:
    @workflow.dynamic_config
    def dynamic_config(self) -> DynamicWorkflowConfig:
        return DynamicWorkflowConfig(
            versioning_behavior=VersioningBehavior.AUTO_UPGRADE
        )

    @workflow.run
    async def run(self, _args: Sequence[RawValue]) -> str:
        return "dynamic"


async def _test_worker_deployment_dynamic_workflow(
    client: Client,
    env: WorkflowEnvironment,
    workflow_class: type[Any],
    expected_versioning_behavior: temporalio.api.enums.v1.VersioningBehavior.ValueType,
):
    if env.supports_time_skipping:
        pytest.skip("Test Server doesn't support worker deployments")

    deployment_name = f"deployment-dynamic-{uuid.uuid4()}"
    worker_v1 = WorkerDeploymentVersion(deployment_name=deployment_name, build_id="1.0")

    async with new_worker(
        client,
        workflow_class,
        deployment_config=WorkerDeploymentConfig(
            version=worker_v1,
            use_worker_versioning=True,
        ),
    ) as w:
        describe_resp = await wait_until_worker_deployment_visible(
            client,
            worker_v1,
        )
        await set_current_deployment_version(
            client, describe_resp.conflict_token, worker_v1
        )

        wf = await client.start_workflow(
            "cooldynamicworkflow",
            id=f"dynamic-workflow-versioning-{uuid.uuid4()}",
            task_queue=w.task_queue,
        )
        result = await wf.result()
        assert result == "dynamic"

        history = await wf.fetch_history()
        assert any(
            event.HasField("workflow_task_completed_event_attributes")
            and event.workflow_task_completed_event_attributes.versioning_behavior
            == expected_versioning_behavior
            for event in history.events
        )


async def test_worker_deployment_dynamic_workflow_with_pinned_versioning(
    client: Client, env: WorkflowEnvironment
):
    await _test_worker_deployment_dynamic_workflow(
        client,
        env,
        DynamicWorkflowVersioningOnDefn,
        temporalio.api.enums.v1.VersioningBehavior.VERSIONING_BEHAVIOR_PINNED,
    )


async def test_worker_deployment_dynamic_workflow_with_auto_upgrade_versioning(
    client: Client, env: WorkflowEnvironment
):
    await _test_worker_deployment_dynamic_workflow(
        client,
        env,
        DynamicWorkflowVersioningOnConfigMethod,
        temporalio.api.enums.v1.VersioningBehavior.VERSIONING_BEHAVIOR_AUTO_UPGRADE,
    )


@workflow.defn
class NoVersioningAnnotationWorkflow:
    @workflow.run
    async def run(self) -> str:
        return "whee"


@workflow.defn(dynamic=True)
class NoVersioningAnnotationDynamicWorkflow:
    @workflow.run
    async def run(self, _args: Sequence[RawValue]) -> str:
        return "whee"


async def test_workflows_must_have_versioning_behavior_when_feature_turned_on(
    client: Client,
):
    with pytest.raises(ValueError) as exc_info:
        Worker(
            client,
            task_queue=f"task-queue-{uuid.uuid4()}",
            workflows=[NoVersioningAnnotationWorkflow],
            deployment_config=WorkerDeploymentConfig(
                version=WorkerDeploymentVersion(
                    deployment_name="whatever", build_id="1.0"
                ),
                use_worker_versioning=True,
            ),
        )

    assert "must specify a versioning behavior" in str(exc_info.value)

    with pytest.raises(ValueError) as exc_info:
        Worker(
            client,
            task_queue=f"task-queue-{uuid.uuid4()}",
            workflows=[NoVersioningAnnotationDynamicWorkflow],
            deployment_config=WorkerDeploymentConfig(
                version=WorkerDeploymentVersion(
                    deployment_name="whatever", build_id="1.0"
                ),
                use_worker_versioning=True,
            ),
        )

    assert "must specify a versioning behavior" in str(exc_info.value)


async def test_workflows_can_use_default_versioning_behavior(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("Test Server doesn't support worker versioning")

    deployment_name = f"deployment-default-versioning-{uuid.uuid4()}"
    worker_v1 = WorkerDeploymentVersion(deployment_name=deployment_name, build_id="1.0")

    async with new_worker(
        client,
        NoVersioningAnnotationWorkflow,
        deployment_config=WorkerDeploymentConfig(
            version=worker_v1,
            use_worker_versioning=True,
            default_versioning_behavior=VersioningBehavior.PINNED,
        ),
    ) as w:
        describe_resp = await wait_until_worker_deployment_visible(
            client,
            worker_v1,
        )
        await set_current_deployment_version(
            client, describe_resp.conflict_token, worker_v1
        )

        wf = await client.start_workflow(
            NoVersioningAnnotationWorkflow.run,
            id=f"default-versioning-behavior-{uuid.uuid4()}",
            task_queue=w.task_queue,
        )
        await wf.result()

        history = await wf.fetch_history()
        assert any(
            event.HasField("workflow_task_completed_event_attributes")
            and event.workflow_task_completed_event_attributes.versioning_behavior
            == temporalio.api.enums.v1.VersioningBehavior.VERSIONING_BEHAVIOR_PINNED
            for event in history.events
        )


async def test_workflows_can_use_versioning_override(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("Test Server doesn't support worker versioning")

    deployment_name = f"deployment-versioning-override-{uuid.uuid4()}"
    worker_v1 = WorkerDeploymentVersion(deployment_name=deployment_name, build_id="1.0")

    async with new_worker(
        client,
        DeploymentVersioningWorkflowV1AutoUpgrade,
        deployment_config=WorkerDeploymentConfig(
            version=worker_v1,
            use_worker_versioning=True,
        ),
    ) as w:
        describe_resp = await wait_until_worker_deployment_visible(
            client,
            worker_v1,
        )
        await set_current_deployment_version(
            client, describe_resp.conflict_token, worker_v1
        )

        handle = await client.start_workflow(
            DeploymentVersioningWorkflowV1AutoUpgrade.run,
            id=f"override-versioning-behavior-{uuid.uuid4()}",
            task_queue=w.task_queue,
            versioning_override=PinnedVersioningOverride(worker_v1),
        )

        await handle.signal(DeploymentVersioningWorkflowV1AutoUpgrade.do_finish)
        await handle.result()

        history = await handle.fetch_history()
        assert any(
            event.HasField("workflow_execution_started_event_attributes")
            and (
                event.workflow_execution_started_event_attributes.versioning_override.behavior
                == temporalio.api.enums.v1.VersioningBehavior.VERSIONING_BEHAVIOR_PINNED
                or event.workflow_execution_started_event_attributes.versioning_override.HasField(
                    "pinned"
                )
            )
            for event in history.events
        )


async def test_can_run_autoscaling_polling_worker(client: Client):
    # Create new runtime with Prom server
    prom_addr = f"127.0.0.1:{find_free_port()}"
    runtime = Runtime(
        telemetry=TelemetryConfig(
            metrics=PrometheusConfig(bind_address=prom_addr),
        )
    )
    client = await Client.connect(
        client.service_client.config.target_host,
        namespace=client.namespace,
        runtime=runtime,
    )

    async with new_worker(
        client,
        WaitOnSignalWorkflow,
        activities=[say_hello],
        workflow_task_poller_behavior=PollerBehaviorAutoscaling(initial=2),
        activity_task_poller_behavior=PollerBehaviorAutoscaling(initial=2),
    ) as w:
        # Give pollers a beat to start
        await asyncio.sleep(0.3)

        with urlopen(url=f"http://{prom_addr}/metrics") as f:
            prom_str: str = f.read().decode("utf-8")
        prom_lines = prom_str.splitlines()
        matches = [line for line in prom_lines if "temporal_num_pollers" in line]
        activity_pollers = [l for l in matches if "activity_task" in l]
        assert len(activity_pollers) == 1
        assert activity_pollers[0].endswith("2")
        workflow_pollers = [
            l for l in matches if "workflow_task" in l and w.task_queue in l
        ]
        assert len(workflow_pollers) == 2
        # There's sticky & non-sticky pollers, and they may have a count of 1 or 2 depending on
        # initialization timing.
        assert workflow_pollers[0].endswith("2") or workflow_pollers[0].endswith("1")
        assert workflow_pollers[1].endswith("2") or workflow_pollers[1].endswith("1")

        async def do_workflow():
            wf = await client.start_workflow(
                WaitOnSignalWorkflow.run,
                id=f"resource-based-{uuid.uuid4()}",
                task_queue=w.task_queue,
            )
            await wf.signal(WaitOnSignalWorkflow.my_signal, "finish")
            await wf.result()

        await asyncio.gather(*[do_workflow() for _ in range(20)])


async def wait_until_worker_deployment_visible(
    client: Client, version: WorkerDeploymentVersion
) -> DescribeWorkerDeploymentResponse:
    async def mk_call() -> DescribeWorkerDeploymentResponse:
        try:
            res = await client.workflow_service.describe_worker_deployment(
                DescribeWorkerDeploymentRequest(
                    namespace=client.namespace,
                    deployment_name=version.deployment_name,
                )
            )
        except RPCError:
            # Expected
            assert False
        assert any(
            vs.version == version.to_canonical_string()
            for vs in res.worker_deployment_info.version_summaries
        )
        return res

    return await assert_eventually(mk_call)


async def set_current_deployment_version(
    client: Client, conflict_token: bytes, version: WorkerDeploymentVersion
) -> SetWorkerDeploymentCurrentVersionResponse:
    return await client.workflow_service.set_worker_deployment_current_version(
        SetWorkerDeploymentCurrentVersionRequest(
            namespace=client.namespace,
            deployment_name=version.deployment_name,
            version=version.to_canonical_string(),
            conflict_token=conflict_token,
        )
    )


async def set_ramping_version(
    client: Client,
    conflict_token: bytes,
    version: WorkerDeploymentVersion,
    percentage: float,
) -> SetWorkerDeploymentRampingVersionResponse:
    response = await client.workflow_service.set_worker_deployment_ramping_version(
        SetWorkerDeploymentRampingVersionRequest(
            namespace=client.namespace,
            deployment_name=version.deployment_name,
            version=version.to_canonical_string(),
            conflict_token=conflict_token,
            percentage=percentage,
        )
    )
    return response


def create_worker(
    client: Client,
    on_fatal_error: Callable[[BaseException], Awaitable[None]] | None = None,
) -> Worker:
    return Worker(
        client,
        task_queue=f"task-queue-{uuid.uuid4()}",
        activities=[never_run_activity],
        workflows=[NeverRunWorkflow],
        nexus_service_handlers=[NeverRunService()],
        on_fatal_error=on_fatal_error,
    )


class WorkerFailureInjector:
    def __init__(self, worker: Worker) -> None:
        self.workflow = PollFailureInjector(worker, "poll_workflow_activation")
        self.activity = PollFailureInjector(worker, "poll_activity_task")

    def __enter__(self) -> WorkerFailureInjector:
        return self

    def __exit__(self, *args: Any, **kwargs: Any) -> None:
        self.workflow.shutdown()
        self.activity.shutdown()


class PollFailureInjector:
    def __init__(self, worker: Worker, attr: str) -> None:
        self.worker = worker
        self.attr = attr
        self.poll_fail_queue: asyncio.Queue[Exception] = asyncio.Queue()
        self.orig_poll_call = getattr(worker._bridge_worker, attr)
        setattr(worker._bridge_worker, attr, self.patched_poll_call)
        self.next_poll_task: asyncio.Task | None = None
        self.next_exception_task: asyncio.Task | None = None

    async def patched_poll_call(self) -> Any:
        if not self.next_poll_task:
            self.next_poll_task = asyncio.create_task(self.orig_poll_call())
        if not self.next_exception_task:
            self.next_exception_task = asyncio.create_task(self.poll_fail_queue.get())

        await asyncio.wait(
            [self.next_poll_task, self.next_exception_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # If activation came, return that and leave queue for next poll
        if self.next_poll_task.done():
            ret = self.next_poll_task.result()
            self.next_poll_task = None
            return ret

        # Raise the error
        exc = self.next_exception_task.result()
        self.next_exception_task = None
        raise exc

    def shutdown(self) -> None:
        if self.next_poll_task:
            self.next_poll_task.cancel()
        if self.next_exception_task:
            self.next_exception_task.cancel()
        setattr(self.worker._bridge_worker, self.attr, self.orig_poll_call)


class TestForkCreateWorker(_TestFork):
    async def coro(self):
        self._worker = Worker(  # type:ignore[reportUninitializedInstanceVariable]
            self._client,
            task_queue=f"task-queue-{uuid.uuid4()}",
            activities=[never_run_activity],
            workflows=[],
            nexus_service_handlers=[],
        )

    def test_fork_create_worker(
        self, client: Client, mp_fork_ctx: multiprocessing.context.BaseContext | None
    ):
        self._expected = _ForkTestResult.assertion_error(
            "Cannot create worker across forks"
        )
        self._client = client  # type:ignore[reportUninitializedInstanceVariable]
        self.run(mp_fork_ctx)


class TestForkUseWorker(_TestFork):
    async def coro(self):
        await self._pre_fork_worker.run()

    def test_fork_use_worker(
        self, client: Client, mp_fork_ctx: multiprocessing.context.BaseContext | None
    ):
        self._expected = _ForkTestResult.assertion_error(
            "Cannot use worker across forks"
        )
        self._pre_fork_worker = Worker(  # type:ignore[reportUninitializedInstanceVariable]
            client,
            task_queue=f"task-queue-{uuid.uuid4()}",
            activities=[never_run_activity],
            workflows=[],
            nexus_service_handlers=[],
        )
        self.run(mp_fork_ctx)
