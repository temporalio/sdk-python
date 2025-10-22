from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum, IntEnum
from typing import Any, Mapping, Optional, Union
from unittest.mock import patch

import temporalio.api.common.v1
import temporalio.api.workflowservice.v1
from temporalio import activity, workflow
from temporalio.client import (
    Client,
    Interceptor,
    OutboundInterceptor,
    StartWorkflowUpdateWithStartInput,
    WithStartWorkflowOperation,
    WorkflowExecutionStatus,
    WorkflowUpdateFailedError,
    WorkflowUpdateHandle,
    WorkflowUpdateStage,
)
from temporalio.common import (
    WorkflowIDConflictPolicy,
    WorkflowIDReusePolicy,
)
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment
from tests.helpers import (
    new_worker,
)

# Passing through because Python <=3.12 has an import bug at
# https://github.com/python/cpython/issues/91351
with workflow.unsafe.imports_passed_through():
    import pytest


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
    client: Client  # type: ignore[reportUninitializedInstanceVariable]
    workflow_id: str  # type: ignore[reportUninitializedInstanceVariable]
    task_queue: str  # type: ignore[reportUninitializedInstanceVariable]
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


async def test_update_with_start_workflow_already_started_error(
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

        def make_start_op(workflow_id: str):
            return WithStartWorkflowOperation(
                WorkflowCanReturnDataClass.run,
                "workflow-arg",
                id=workflow_id,
                task_queue=worker.task_queue,
                id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            )

        # no-param update-function overload
        start_op = make_start_op(f"wf-{uuid.uuid4()}")

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
        start_op = make_start_op(f"wf-{uuid.uuid4()}")

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


@dataclass
class WorkflowResult:
    result: str


@dataclass
class UpdateResult:
    result: str


@workflow.defn
class NoParamWorkflow:
    def __init__(self) -> None:
        self.received_update = False

    @workflow.run
    async def my_workflow_run(self) -> WorkflowResult:
        await workflow.wait_condition(lambda: self.received_update)
        return WorkflowResult(result="workflow-result")

    @workflow.update(name="my_update")
    async def update(self) -> UpdateResult:
        self.received_update = True
        return UpdateResult(result="update-result")


@workflow.defn
class OneParamWorkflow:
    def __init__(self) -> None:
        self.received_update = False

    @workflow.run
    async def my_workflow_run(self, arg: str) -> WorkflowResult:
        await workflow.wait_condition(lambda: self.received_update)
        return WorkflowResult(result=arg)

    @workflow.update(name="my_update")
    async def update(self, arg: str) -> UpdateResult:
        self.received_update = True
        return UpdateResult(result=arg)


@workflow.defn
class TwoParamWorkflow:
    def __init__(self) -> None:
        self.received_update = False

    @workflow.run
    async def my_workflow_run(self, arg1: str, arg2: str) -> WorkflowResult:
        await workflow.wait_condition(lambda: self.received_update)
        return WorkflowResult(result=arg1 + "-" + arg2)

    @workflow.update(name="my_update")
    async def update(self, arg1: str, arg2: str) -> UpdateResult:
        self.received_update = True
        return UpdateResult(result=arg1 + "-" + arg2)


async def test_update_with_start_no_param(client: Client):
    async with new_worker(client, NoParamWorkflow) as worker:
        # No-params typed
        no_param_start_op = WithStartWorkflowOperation(
            NoParamWorkflow.my_workflow_run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )
        update_handle = await client.start_update_with_start_workflow(
            NoParamWorkflow.update,
            wait_for_stage=WorkflowUpdateStage.COMPLETED,
            start_workflow_operation=no_param_start_op,
        )
        assert await update_handle.result() == UpdateResult(result="update-result")
        wf_handle = await no_param_start_op.workflow_handle()
        assert await wf_handle.result() == WorkflowResult(result="workflow-result")

        # No-params string name
        no_param_start_op = WithStartWorkflowOperation(
            "NoParamWorkflow",
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            result_type=WorkflowResult,
        )
        update_handle = await client.start_update_with_start_workflow(
            "my_update",
            wait_for_stage=WorkflowUpdateStage.COMPLETED,
            start_workflow_operation=no_param_start_op,
            result_type=UpdateResult,
        )
        assert await update_handle.result() == UpdateResult(result="update-result")
        wf_handle = await no_param_start_op.workflow_handle()
        assert await wf_handle.result() == WorkflowResult(result="workflow-result")


async def test_update_with_start_one_param(client: Client):
    async with new_worker(client, OneParamWorkflow) as worker:
        # One-param typed
        one_param_start_op = WithStartWorkflowOperation(
            OneParamWorkflow.my_workflow_run,
            "workflow-arg",
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )
        update_handle = await client.start_update_with_start_workflow(
            OneParamWorkflow.update,
            "update-arg",
            wait_for_stage=WorkflowUpdateStage.COMPLETED,
            start_workflow_operation=one_param_start_op,
        )
        assert await update_handle.result() == UpdateResult(result="update-arg")
        wf_handle = await one_param_start_op.workflow_handle()
        assert await wf_handle.result() == WorkflowResult(result="workflow-arg")

        # One-param string name
        one_param_start_op = WithStartWorkflowOperation(
            "OneParamWorkflow",
            "workflow-arg",
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            result_type=WorkflowResult,
        )
        update_handle = await client.start_update_with_start_workflow(
            "my_update",
            "update-arg",
            wait_for_stage=WorkflowUpdateStage.COMPLETED,
            start_workflow_operation=one_param_start_op,
            result_type=UpdateResult,
        )
        assert await update_handle.result() == UpdateResult(result="update-arg")
        wf_handle = await one_param_start_op.workflow_handle()
        assert await wf_handle.result() == WorkflowResult(result="workflow-arg")


async def test_update_with_start_two_param(client: Client):
    async with new_worker(client, TwoParamWorkflow) as worker:
        # Two-params typed
        two_param_start_op = WithStartWorkflowOperation(
            TwoParamWorkflow.my_workflow_run,
            args=("workflow-arg1", "workflow-arg2"),
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )
        update_handle = await client.start_update_with_start_workflow(
            TwoParamWorkflow.update,
            args=("update-arg1", "update-arg2"),
            wait_for_stage=WorkflowUpdateStage.COMPLETED,
            start_workflow_operation=two_param_start_op,
        )
        assert await update_handle.result() == UpdateResult(
            result="update-arg1-update-arg2"
        )
        wf_handle = await two_param_start_op.workflow_handle()
        assert await wf_handle.result() == WorkflowResult(
            result="workflow-arg1-workflow-arg2"
        )

        # Two-params string name
        two_param_start_op = WithStartWorkflowOperation(
            "TwoParamWorkflow",
            args=("workflow-arg1", "workflow-arg2"),
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            result_type=WorkflowResult,
        )
        update_handle = await client.start_update_with_start_workflow(
            "my_update",
            args=("update-arg1", "update-arg2"),
            wait_for_stage=WorkflowUpdateStage.COMPLETED,
            start_workflow_operation=two_param_start_op,
            result_type=UpdateResult,
        )
        assert await update_handle.result() == UpdateResult(
            result="update-arg1-update-arg2"
        )
        wf_handle = await two_param_start_op.workflow_handle()
        assert await wf_handle.result() == WorkflowResult(
            result="workflow-arg1-workflow-arg2"
        )


# Verify correcting issue #791
async def test_start_update_with_start_empty_details(client: Client):
    class execute_multi_operation:
        empty_details_err = RPCError("empty details", RPCStatusCode.INTERNAL, b"")
        # Set grpc_status with empty details
        empty_details_err._grpc_status = temporalio.api.common.v1.GrpcStatus(details=[])

        def __init__(self) -> None:  # type: ignore[reportMissingSuperCall]
            pass

        async def __call__(
            self,
            req: temporalio.api.workflowservice.v1.ExecuteMultiOperationRequest,
            *,
            retry: bool = False,
            metadata: Mapping[str, Union[str, bytes]] = {},
            timeout: Optional[timedelta] = None,
        ) -> temporalio.api.workflowservice.v1.ExecuteMultiOperationResponse:
            raise self.empty_details_err

    with patch.object(
        client.workflow_service, "execute_multi_operation", execute_multi_operation()
    ):
        start_workflow_operation = WithStartWorkflowOperation(
            UpdateWithStartInterceptorWorkflow.run,
            "wf-arg",
            id=f"wf-{uuid.uuid4()}",
            task_queue="tq",
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )
        with pytest.raises(RPCError) as err:
            await client.start_update_with_start_workflow(
                UpdateWithStartInterceptorWorkflow.my_update,
                "original-update-arg",
                start_workflow_operation=start_workflow_operation,
                wait_for_stage=WorkflowUpdateStage.ACCEPTED,
            )
        _ = start_workflow_operation._workflow_handle.exception()
        assert err.value.status == RPCStatusCode.INTERNAL
        assert err.value.message == "empty details"
        assert len(err.value.grpc_status.details) == 0


class ExecutionBehavior(IntEnum):
    COMPLETES = 0
    BLOCKS = 1


@workflow.defn
class WorkflowWithUpdate:
    def __init__(self) -> None:
        self._unblock_workflow = asyncio.Event()
        self._unblock_update = asyncio.Event()

    @workflow.run
    async def run(self, behavior: ExecutionBehavior) -> str:
        if behavior == ExecutionBehavior.BLOCKS:
            await self._unblock_workflow.wait()
        return str(workflow.uuid4())

    @workflow.update(unfinished_policy=workflow.HandlerUnfinishedPolicy.ABANDON)
    async def update(self, behavior: ExecutionBehavior) -> str:
        if behavior == ExecutionBehavior.BLOCKS:
            await self._unblock_update.wait()
        return str(workflow.uuid4())

    @workflow.signal
    async def unblock_workflow(self):
        self._unblock_workflow.set()

    @workflow.signal
    async def unblock_update(self):
        self._unblock_update.set()


@pytest.mark.parametrize(
    "workflow_behavior_name",
    [ExecutionBehavior.COMPLETES.name, ExecutionBehavior.BLOCKS.name],
)
@pytest.mark.parametrize(
    "id_conflict_policy_name",
    [
        WorkflowIDConflictPolicy.USE_EXISTING.name,
        WorkflowIDConflictPolicy.FAIL.name,
    ],
)
@pytest.mark.parametrize(
    "id_reuse_policy_name",
    [
        WorkflowIDReusePolicy.ALLOW_DUPLICATE.name,
        WorkflowIDReusePolicy.REJECT_DUPLICATE.name,
    ],
)
async def test_update_with_start_always_attaches_to_completed_update(
    env: WorkflowEnvironment,
    workflow_behavior_name: str,
    id_conflict_policy_name: str,
    id_reuse_policy_name: str,
):
    """
    A workflow exists and contains a completed update. An update-with-start sent for that workflow ID and that
    update ID attaches to the update if workflow is running. If the workflow is closed then it attaches iff
    the update is completed. The behavior is unaffected by the conflict policy or id reuse policy (so, for
    example, we attach to an update in an existing workflow even if the conflict policy is FAIL).
    """
    if env.supports_time_skipping:
        pytest.skip("TODO: make update_with_start tests pass under Java test server")
    client = env.client
    id_conflict_policy = WorkflowIDConflictPolicy[id_conflict_policy_name]
    id_reuse_policy = WorkflowIDReusePolicy[id_reuse_policy_name]
    workflow_behavior = ExecutionBehavior[workflow_behavior_name]
    shared_workflow_id = f"workflow-id-{uuid.uuid4()}"
    shared_update_id = f"update-id-{uuid.uuid4()}"
    async with new_worker(client, WorkflowWithUpdate) as worker:

        def start_op():
            return WithStartWorkflowOperation(
                WorkflowWithUpdate.run,
                workflow_behavior,
                id=shared_workflow_id,
                task_queue=worker.task_queue,
                id_conflict_policy=id_conflict_policy,
                id_reuse_policy=id_reuse_policy,
            )

        start_op_1 = start_op()
        update_result_1 = await client.execute_update_with_start_workflow(
            WorkflowWithUpdate.update,
            ExecutionBehavior.COMPLETES,
            id=shared_update_id,
            start_workflow_operation=start_op_1,
        )
        wf_handle_1 = await start_op_1.workflow_handle()
        assert (await wf_handle_1.describe()).status == (
            WorkflowExecutionStatus.COMPLETED
            if workflow_behavior == ExecutionBehavior.COMPLETES
            else WorkflowExecutionStatus.RUNNING
        )

        # Whether or not the workflow closed, the update exists in the last workflow run and is completed, so
        # we attach to it.

        start_op_2 = start_op()
        update_result_2 = await client.execute_update_with_start_workflow(
            WorkflowWithUpdate.update,
            ExecutionBehavior.COMPLETES,
            id=shared_update_id,
            start_workflow_operation=start_op_2,
        )
        wf_handle_2 = await start_op_2.workflow_handle()
        assert wf_handle_1.first_execution_run_id == wf_handle_2.first_execution_run_id
        assert update_result_1 == update_result_2


@pytest.mark.parametrize(
    "id_conflict_policy_name",
    [
        WorkflowIDConflictPolicy.USE_EXISTING.name,
        WorkflowIDConflictPolicy.FAIL.name,
    ],
)
@pytest.mark.parametrize(
    "id_reuse_policy_name",
    [
        WorkflowIDReusePolicy.ALLOW_DUPLICATE.name,
        WorkflowIDReusePolicy.REJECT_DUPLICATE.name,
    ],
)
async def test_update_with_start_attaches_to_non_completed_update_in_running_workflow(
    env: WorkflowEnvironment,
    id_conflict_policy_name: str,
    id_reuse_policy_name: str,
):
    """
    A workflow exists and is running and contains a non-completed update. An update-with-start sent for that
    workflow ID and that update ID attaches to the update. The behavior is unaffected by the conflict policy
    or id reuse policy (so, for example, we attach to the update in an existing workflow even if the conflict
    policy is FAIL).
    """
    if env.supports_time_skipping:
        pytest.skip("TODO: make update_with_start tests pass under Java test server")
    client = env.client
    id_conflict_policy = WorkflowIDConflictPolicy[id_conflict_policy_name]
    id_reuse_policy = WorkflowIDReusePolicy[id_reuse_policy_name]
    shared_workflow_id = f"workflow-id-{uuid.uuid4()}"
    shared_update_id = f"update-id-{uuid.uuid4()}"
    async with new_worker(client, WorkflowWithUpdate) as worker:

        def start_op():
            return WithStartWorkflowOperation(
                WorkflowWithUpdate.run,
                ExecutionBehavior.BLOCKS,
                id=shared_workflow_id,
                task_queue=worker.task_queue,
                id_conflict_policy=id_conflict_policy,
                id_reuse_policy=id_reuse_policy,
            )

        start_op_1 = start_op()
        update_handle_1 = await client.start_update_with_start_workflow(
            WorkflowWithUpdate.update,
            ExecutionBehavior.BLOCKS,
            id=shared_update_id,
            start_workflow_operation=start_op_1,
            wait_for_stage=WorkflowUpdateStage.ACCEPTED,
        )
        wf_handle_1 = await start_op_1.workflow_handle()
        assert (await wf_handle_1.describe()).status == WorkflowExecutionStatus.RUNNING

        # The workflow is running with the update not-completed. We will attach to the update.

        start_op_2 = start_op()

        update_handle_2 = await client.start_update_with_start_workflow(
            WorkflowWithUpdate.update,
            ExecutionBehavior.COMPLETES,
            id=shared_update_id,
            start_workflow_operation=start_op_2,
            wait_for_stage=WorkflowUpdateStage.ACCEPTED,
        )
        wf_handle_2 = await start_op_2.workflow_handle()
        assert wf_handle_1.first_execution_run_id == wf_handle_2.first_execution_run_id
        await wf_handle_1.signal(WorkflowWithUpdate.unblock_update)
        assert (await update_handle_1.result()) == (await update_handle_2.result())


@pytest.mark.parametrize(
    "id_conflict_policy_name",
    [
        WorkflowIDConflictPolicy.USE_EXISTING.name,
        WorkflowIDConflictPolicy.FAIL.name,
    ],
)
@pytest.mark.parametrize(
    "id_reuse_policy_name",
    [
        WorkflowIDReusePolicy.ALLOW_DUPLICATE.name,
        WorkflowIDReusePolicy.REJECT_DUPLICATE.name,
    ],
)
async def test_update_with_start_does_not_attach_to_non_completed_update_in_closed_workflow(
    env: WorkflowEnvironment,
    id_conflict_policy_name: str,
    id_reuse_policy_name: str,
):
    """
    A workflow exists but is closed and contains a non-completed update. An update-with-start sent for that workflow
    ID and that update ID does not attach to the update. If the id reuse policy is ALLOW_DUPLICATE then a new
    workflow is started and the update is issued.
    """
    if env.supports_time_skipping:
        pytest.skip("TODO: make update_with_start tests pass under Java test server")
    client = env.client
    id_conflict_policy = WorkflowIDConflictPolicy[id_conflict_policy_name]
    id_reuse_policy = WorkflowIDReusePolicy[id_reuse_policy_name]
    shared_workflow_id = f"workflow-id-{uuid.uuid4()}"
    shared_update_id = f"update-id-{uuid.uuid4()}"
    async with new_worker(client, WorkflowWithUpdate) as worker:

        def start_op():
            return WithStartWorkflowOperation(
                WorkflowWithUpdate.run,
                ExecutionBehavior.COMPLETES,
                id=shared_workflow_id,
                task_queue=worker.task_queue,
                id_conflict_policy=id_conflict_policy,
                id_reuse_policy=id_reuse_policy,
            )

        start_op_1 = start_op()
        await client.start_update_with_start_workflow(
            WorkflowWithUpdate.update,
            ExecutionBehavior.BLOCKS,
            id=shared_update_id,
            start_workflow_operation=start_op_1,
            wait_for_stage=WorkflowUpdateStage.ACCEPTED,
        )
        wf_handle_1 = await start_op_1.workflow_handle()
        assert (
            await wf_handle_1.describe()
        ).status == WorkflowExecutionStatus.COMPLETED

        # The workflow closed with the update not-completed. We will start a new workflow and issue the update
        # iff reuse_policy is ALLOW_DUPLICATE. Conflict policy is irrelevant.

        start_op_2 = start_op()

        async def _do_update() -> Any:
            return await client.execute_update_with_start_workflow(
                WorkflowWithUpdate.update,
                ExecutionBehavior.COMPLETES,
                id=shared_update_id,
                start_workflow_operation=start_op_2,
            )

        if id_reuse_policy == WorkflowIDReusePolicy.ALLOW_DUPLICATE:
            await _do_update()
            wf_handle_2 = await start_op_2.workflow_handle()
            assert (
                wf_handle_1.first_execution_run_id != wf_handle_2.first_execution_run_id
            )
        elif id_reuse_policy == WorkflowIDReusePolicy.REJECT_DUPLICATE:
            with pytest.raises(WorkflowAlreadyStartedError):
                await _do_update()
