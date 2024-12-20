from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import Any, Iterator
from unittest.mock import patch

import pytest

from temporalio import activity, workflow
from temporalio.client import (
    Client,
    Interceptor,
    OutboundInterceptor,
    StartWorkflowUpdateWithStartInput,
    WithStartWorkflowOperation,
    WorkflowUpdateFailedError,
    WorkflowUpdateHandle,
    WorkflowUpdateStage,
)
from temporalio.common import (
    WorkflowIDConflictPolicy,
)
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.testing import WorkflowEnvironment
from tests.helpers import (
    new_worker,
)


@activity.defn
async def activity_called_by_update() -> None:
    pass


@workflow.defn
class WorkflowForUpdateWithStartTest:
    def __init__(self) -> None:
        self.update_finished = False
        self.update_may_exit = False
        self.received_done_signal = False

    @workflow.run
    async def run(self, i: int) -> str:
        await workflow.wait_condition(lambda: self.received_done_signal)
        return f"workflow-result-{i}"

    @workflow.update
    def my_non_blocking_update(self, s: str) -> str:
        if s == "fail-after-acceptance":
            raise ApplicationError("Workflow deliberate failed update")
        return f"update-result-{s}"

    @workflow.update
    async def my_blocking_update(self, s: str) -> str:
        if s == "fail-after-acceptance":
            raise ApplicationError("Workflow deliberate failed update")
        await workflow.execute_activity(
            activity_called_by_update, start_to_close_timeout=timedelta(seconds=10)
        )
        return f"update-result-{s}"

    @workflow.signal
    async def done(self):
        self.received_done_signal = True


async def test_with_start_workflow_operation_cannot_be_reused(client: Client):
    async with new_worker(client, WorkflowForUpdateWithStartTest) as worker:
        start_op = WithStartWorkflowOperation(
            WorkflowForUpdateWithStartTest.run,
            0,
            id=f"wid-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )

        async def start_update_with_start(start_op: WithStartWorkflowOperation):
            return await client.start_update_with_start_workflow(
                WorkflowForUpdateWithStartTest.my_non_blocking_update,
                "1",
                wait_for_stage=WorkflowUpdateStage.COMPLETED,
                start_workflow_operation=start_op,
            )

        await start_update_with_start(start_op)
        with pytest.raises(RuntimeError) as exc_info:
            await start_update_with_start(start_op)
        assert "WithStartWorkflowOperation cannot be reused" in str(exc_info.value)


class ExpectErrorWhenWorkflowExists(Enum):
    YES = "yes"
    NO = "no"


class UpdateHandlerType(Enum):
    NON_BLOCKING = "non-blocking"
    BLOCKING = "blocking"


class TestUpdateWithStart:
    client: Client
    workflow_id: str
    task_queue: str
    update_id = "test-uws-up-id"

    @pytest.mark.parametrize(
        "wait_for_stage",
        [WorkflowUpdateStage.ACCEPTED, WorkflowUpdateStage.COMPLETED],
    )
    async def test_non_blocking_update_with_must_create_workflow_semantics(
        self,
        client: Client,
        env: WorkflowEnvironment,
        wait_for_stage: WorkflowUpdateStage,
    ):
        if env.supports_time_skipping:
            pytest.skip(
                "TODO: make update_with_start_tests pass under Java test server"
            )
        await self._do_test(
            client,
            f"test-uws-nb-mc-wf-id-{wait_for_stage.name}",
            UpdateHandlerType.NON_BLOCKING,
            wait_for_stage,
            WorkflowIDConflictPolicy.FAIL,
            ExpectErrorWhenWorkflowExists.YES,
        )

    @pytest.mark.parametrize(
        "wait_for_stage",
        [WorkflowUpdateStage.ACCEPTED, WorkflowUpdateStage.COMPLETED],
    )
    async def test_non_blocking_update_with_get_or_create_workflow_semantics(
        self,
        client: Client,
        env: WorkflowEnvironment,
        wait_for_stage: WorkflowUpdateStage,
    ):
        if env.supports_time_skipping:
            pytest.skip(
                "TODO: make update_with_start_tests pass under Java test server"
            )
        await self._do_test(
            client,
            f"test-uws-nb-goc-wf-id-{wait_for_stage.name}",
            UpdateHandlerType.NON_BLOCKING,
            wait_for_stage,
            WorkflowIDConflictPolicy.USE_EXISTING,
            ExpectErrorWhenWorkflowExists.NO,
        )

    @pytest.mark.parametrize(
        "wait_for_stage",
        [WorkflowUpdateStage.ACCEPTED, WorkflowUpdateStage.COMPLETED],
    )
    async def test_blocking_update_with_get_or_create_workflow_semantics(
        self,
        client: Client,
        env: WorkflowEnvironment,
        wait_for_stage: WorkflowUpdateStage,
    ):
        if env.supports_time_skipping:
            pytest.skip(
                "TODO: make update_with_start_tests pass under Java test server"
            )
        await self._do_test(
            client,
            f"test-uws-b-goc-wf-id-{wait_for_stage.name}",
            UpdateHandlerType.BLOCKING,
            wait_for_stage,
            WorkflowIDConflictPolicy.USE_EXISTING,
            ExpectErrorWhenWorkflowExists.NO,
        )

    async def _do_test(
        self,
        client: Client,
        workflow_id: str,
        update_handler_type: UpdateHandlerType,
        wait_for_stage: WorkflowUpdateStage,
        id_conflict_policy: WorkflowIDConflictPolicy,
        expect_error_when_workflow_exists: ExpectErrorWhenWorkflowExists,
    ):
        await self._do_execute_update_test(
            client,
            workflow_id + "-execute-update",
            update_handler_type,
            id_conflict_policy,
            expect_error_when_workflow_exists,
        )
        await self._do_start_update_test(
            client,
            workflow_id + "-start-update",
            update_handler_type,
            wait_for_stage,
            id_conflict_policy,
        )

    async def _do_execute_update_test(
        self,
        client: Client,
        workflow_id: str,
        update_handler_type: UpdateHandlerType,
        id_conflict_policy: WorkflowIDConflictPolicy,
        expect_error_when_workflow_exists: ExpectErrorWhenWorkflowExists,
    ):
        update_handler = (
            WorkflowForUpdateWithStartTest.my_blocking_update
            if update_handler_type == UpdateHandlerType.BLOCKING
            else WorkflowForUpdateWithStartTest.my_non_blocking_update
        )
        async with new_worker(
            client,
            WorkflowForUpdateWithStartTest,
            activities=[activity_called_by_update],
        ) as worker:
            self.client = client
            self.workflow_id = workflow_id
            self.task_queue = worker.task_queue

            start_op_1 = WithStartWorkflowOperation(
                WorkflowForUpdateWithStartTest.run,
                1,
                id=self.workflow_id,
                task_queue=self.task_queue,
                id_conflict_policy=id_conflict_policy,
            )

            # First UWS succeeds
            assert (
                await client.execute_update_with_start_workflow(
                    update_handler, "1", start_workflow_operation=start_op_1
                )
                == "update-result-1"
            )
            assert (
                await start_op_1.workflow_handle()
            ).first_execution_run_id is not None

            # Whether a repeat UWS succeeds depends on the workflow ID conflict policy
            start_op_2 = WithStartWorkflowOperation(
                WorkflowForUpdateWithStartTest.run,
                2,
                id=self.workflow_id,
                task_queue=self.task_queue,
                id_conflict_policy=id_conflict_policy,
            )

            if expect_error_when_workflow_exists == ExpectErrorWhenWorkflowExists.NO:
                assert (
                    await client.execute_update_with_start_workflow(
                        update_handler, "21", start_workflow_operation=start_op_2
                    )
                    == "update-result-21"
                )
                assert (
                    await start_op_2.workflow_handle()
                ).first_execution_run_id is not None
            else:
                for aw in [
                    client.execute_update_with_start_workflow(
                        update_handler, "21", start_workflow_operation=start_op_2
                    ),
                    start_op_2.workflow_handle(),
                ]:
                    with pytest.raises(WorkflowAlreadyStartedError):
                        await aw

                assert (
                    await start_op_1.workflow_handle()
                ).first_execution_run_id is not None

            # The workflow is still running; finish it.

            wf_handle_1 = await start_op_1.workflow_handle()
            await wf_handle_1.signal(WorkflowForUpdateWithStartTest.done)
            assert await wf_handle_1.result() == "workflow-result-1"

    async def _do_start_update_test(
        self,
        client: Client,
        workflow_id: str,
        update_handler_type: UpdateHandlerType,
        wait_for_stage: WorkflowUpdateStage,
        id_conflict_policy: WorkflowIDConflictPolicy,
    ):
        update_handler = (
            WorkflowForUpdateWithStartTest.my_blocking_update
            if update_handler_type == UpdateHandlerType.BLOCKING
            else WorkflowForUpdateWithStartTest.my_non_blocking_update
        )
        async with new_worker(
            client,
            WorkflowForUpdateWithStartTest,
            activities=[activity_called_by_update],
        ) as worker:
            self.client = client
            self.workflow_id = workflow_id
            self.task_queue = worker.task_queue

            start_op = WithStartWorkflowOperation(
                WorkflowForUpdateWithStartTest.run,
                1,
                id=self.workflow_id,
                task_queue=self.task_queue,
                id_conflict_policy=id_conflict_policy,
            )

            update_handle = await client.start_update_with_start_workflow(
                update_handler,
                "1",
                wait_for_stage=wait_for_stage,
                start_workflow_operation=start_op,
            )
            assert await update_handle.result() == "update-result-1"

    @contextmanager
    def assert_network_call(
        self,
        expect_network_call: bool,
    ) -> Iterator[None]:
        with patch.object(
            self.client.workflow_service,
            "poll_workflow_execution_update",
            wraps=self.client.workflow_service.poll_workflow_execution_update,
        ) as _wrapped_poll:
            yield
            assert _wrapped_poll.called == expect_network_call


async def test_update_with_start_sets_first_execution_run_id(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("TODO: make update_with_start_tests pass under Java test server")
    async with new_worker(
        client,
        WorkflowForUpdateWithStartTest,
        activities=[activity_called_by_update],
    ) as worker:

        def make_start_op(workflow_id: str):
            return WithStartWorkflowOperation(
                WorkflowForUpdateWithStartTest.run,
                0,
                id=workflow_id,
                id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
                task_queue=worker.task_queue,
            )

        # conflict policy is FAIL
        # First UWS succeeds and sets the first execution run ID
        start_op_1 = make_start_op("wid-1")
        update_handle_1 = await client.start_update_with_start_workflow(
            WorkflowForUpdateWithStartTest.my_non_blocking_update,
            "1",
            wait_for_stage=WorkflowUpdateStage.COMPLETED,
            start_workflow_operation=start_op_1,
        )
        assert (await start_op_1.workflow_handle()).first_execution_run_id is not None
        assert await update_handle_1.result() == "update-result-1"

        # Second UWS start fails because the workflow already exists
        # first execution run ID is not set on the second UWS handle
        start_op_2 = make_start_op("wid-1")

        for aw in [
            client.start_update_with_start_workflow(
                WorkflowForUpdateWithStartTest.my_non_blocking_update,
                "2",
                wait_for_stage=WorkflowUpdateStage.COMPLETED,
                start_workflow_operation=start_op_2,
            ),
            start_op_2.workflow_handle(),
        ]:
            with pytest.raises(WorkflowAlreadyStartedError):
                await aw

        # Third UWS start succeeds, but the update fails after acceptance
        start_op_3 = make_start_op("wid-2")
        update_handle_3 = await client.start_update_with_start_workflow(
            WorkflowForUpdateWithStartTest.my_non_blocking_update,
            "fail-after-acceptance",
            wait_for_stage=WorkflowUpdateStage.COMPLETED,
            start_workflow_operation=start_op_3,
        )
        assert (await start_op_3.workflow_handle()).first_execution_run_id is not None
        with pytest.raises(WorkflowUpdateFailedError):
            await update_handle_3.result()

        # Despite the update failure, the first execution run ID is set on the with_start_request,
        # and the handle can be used to obtain the workflow result.
        assert (await start_op_3.workflow_handle()).first_execution_run_id is not None
        wf_handle_3 = await start_op_3.workflow_handle()
        await wf_handle_3.signal(WorkflowForUpdateWithStartTest.done)
        assert await wf_handle_3.result() == "workflow-result-0"

        # Fourth UWS is same as third, but we use execute_update instead of start_update.
        start_op_4 = make_start_op("wid-3")
        with pytest.raises(WorkflowUpdateFailedError):
            await client.execute_update_with_start_workflow(
                WorkflowForUpdateWithStartTest.my_non_blocking_update,
                "fail-after-acceptance",
                start_workflow_operation=start_op_4,
            )
        assert (await start_op_4.workflow_handle()).first_execution_run_id is not None


async def test_update_with_start_failure_start_workflow_error(
    client: Client, env: WorkflowEnvironment
):
    """
    When the workflow start fails, the update_with_start_call should raise the appropriate
    gRPC error, and the start_workflow_operation promise should be rejected with the same
    error.
    """
    if env.supports_time_skipping:
        pytest.skip("TODO: make update_with_start_tests pass under Java test server")
    async with new_worker(
        client,
        WorkflowForUpdateWithStartTest,
    ) as worker:

        def make_start_op(workflow_id: str):
            return WithStartWorkflowOperation(
                WorkflowForUpdateWithStartTest.run,
                0,
                id=workflow_id,
                id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
                task_queue=worker.task_queue,
            )

        wid = f"wf-{uuid.uuid4()}"
        start_op_1 = make_start_op(wid)
        await client.start_update_with_start_workflow(
            WorkflowForUpdateWithStartTest.my_non_blocking_update,
            "1",
            wait_for_stage=WorkflowUpdateStage.COMPLETED,
            start_workflow_operation=start_op_1,
        )

        start_op_2 = make_start_op(wid)

        for aw in [
            client.start_update_with_start_workflow(
                WorkflowForUpdateWithStartTest.my_non_blocking_update,
                "2",
                wait_for_stage=WorkflowUpdateStage.COMPLETED,
                start_workflow_operation=start_op_2,
            ),
            start_op_2.workflow_handle(),
        ]:
            with pytest.raises(WorkflowAlreadyStartedError):
                await aw


class SimpleClientInterceptor(Interceptor):
    def intercept_client(self, next: OutboundInterceptor) -> OutboundInterceptor:
        return SimpleClientOutboundInterceptor(super().intercept_client(next))


class SimpleClientOutboundInterceptor(OutboundInterceptor):
    def __init__(self, next: OutboundInterceptor) -> None:
        super().__init__(next)

    async def start_update_with_start_workflow(
        self, input: StartWorkflowUpdateWithStartInput
    ) -> WorkflowUpdateHandle[Any]:
        input.start_workflow_input.args = ["intercepted-workflow-arg"]
        input.update_workflow_input.args = ["intercepted-update-arg"]
        return await super().start_update_with_start_workflow(input)


@workflow.defn
class UpdateWithStartInterceptorWorkflow:
    def __init__(self) -> None:
        self.received_update = False

    @workflow.run
    async def run(self, arg: str) -> str:
        await workflow.wait_condition(lambda: self.received_update)
        return arg

    @workflow.update
    async def my_update(self, arg: str) -> str:
        self.received_update = True
        await workflow.wait_condition(lambda: self.received_update)
        return arg


async def test_update_with_start_client_outbound_interceptor(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("TODO: make update_with_start_tests pass under Java test server")
    interceptor = SimpleClientInterceptor()
    client = Client(**{**client.config(), "interceptors": [interceptor]})  # type: ignore

    async with new_worker(
        client,
        UpdateWithStartInterceptorWorkflow,
    ) as worker:
        start_op = WithStartWorkflowOperation(
            UpdateWithStartInterceptorWorkflow.run,
            "original-workflow-arg",
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )
        update_result = await client.execute_update_with_start_workflow(
            UpdateWithStartInterceptorWorkflow.my_update,
            "original-update-arg",
            start_workflow_operation=start_op,
        )
        assert update_result == "intercepted-update-arg"

        wf_handle = await start_op.workflow_handle()
        assert await wf_handle.result() == "intercepted-workflow-arg"


def test_with_start_workflow_operation_requires_conflict_policy():
    with pytest.raises(ValueError):
        WithStartWorkflowOperation(
            WorkflowForUpdateWithStartTest.run,
            0,
            id="wid-1",
            id_conflict_policy=WorkflowIDConflictPolicy.UNSPECIFIED,
            task_queue="test-queue",
        )

    with pytest.raises(TypeError):
        WithStartWorkflowOperation(  # type: ignore
            WorkflowForUpdateWithStartTest.run,
            0,
            id="wid-1",
            task_queue="test-queue",
        )


@dataclass
class DataClass1:
    a: str
    b: str


@dataclass
class DataClass2:
    a: str
    b: str


@workflow.defn
class WorkflowCanReturnDataClass:
    def __init__(self) -> None:
        self.received_update = False

    @workflow.run
    async def run(self, arg: str) -> DataClass1:
        await workflow.wait_condition(lambda: self.received_update)
        return DataClass1(a=arg, b="workflow-result")

    @workflow.update
    async def my_update(self, arg: str) -> DataClass2:
        self.received_update = True
        return DataClass2(a=arg, b="update-result")


async def test_workflow_and_update_can_return_dataclass(client: Client):
    async with new_worker(client, WorkflowCanReturnDataClass) as worker:
        make_start_op = lambda: WithStartWorkflowOperation(
            WorkflowCanReturnDataClass.run,
            "workflow-arg",
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )

        # no-param update-function overload
        start_op = make_start_op()

        update_handle = await client.start_update_with_start_workflow(
            WorkflowCanReturnDataClass.my_update,
            "update-arg",
            wait_for_stage=WorkflowUpdateStage.COMPLETED,
            start_workflow_operation=start_op,
        )

        assert await update_handle.result() == DataClass2(
            a="update-arg", b="update-result"
        )

        wf_handle = await start_op.workflow_handle()
        assert await wf_handle.result() == DataClass1(
            a="workflow-arg", b="workflow-result"
        )

        # no-param update-string-name overload
        start_op = make_start_op()

        update_handle = await client.start_update_with_start_workflow(
            "my_update",
            "update-arg",
            wait_for_stage=WorkflowUpdateStage.COMPLETED,
            start_workflow_operation=start_op,
            result_type=DataClass2,
        )

        assert await update_handle.result() == DataClass2(
            a="update-arg", b="update-result"
        )

        wf_handle = await start_op.workflow_handle()
        assert await wf_handle.result() == DataClass1(
            a="workflow-arg", b="workflow-result"
        )
