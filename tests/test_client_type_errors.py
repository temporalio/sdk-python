"""
This file exists to test for type-checker false positives and false negatives.
It doesn't contain any test functions.
"""

from dataclasses import dataclass
from unittest.mock import Mock

from temporalio import workflow
from temporalio.client import (
    Client,
    WithStartWorkflowOperation,
    WorkflowHandle,
    WorkflowUpdateHandle,
    WorkflowUpdateStage,
)
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.service import ServiceClient


@dataclass
class WorkflowInput:
    pass


@dataclass
class SignalInput:
    pass


@dataclass
class QueryInput:
    pass


@dataclass
class UpdateInput:
    pass


@dataclass
class WorkflowOutput:
    pass


@dataclass
class QueryOutput:
    pass


@dataclass
class UpdateOutput:
    pass


@workflow.defn
class TestWorkflow:
    @workflow.run
    async def run(self, _: WorkflowInput) -> WorkflowOutput:
        return WorkflowOutput()

    @workflow.signal
    async def signal(self, _: SignalInput) -> None:
        pass

    @workflow.query
    async def query(self, _: QueryInput) -> QueryOutput:
        return QueryOutput()

    @workflow.update
    async def update(self, _: UpdateInput) -> UpdateOutput:
        return UpdateOutput()


@workflow.defn
class TestWorkflow2(TestWorkflow):
    @workflow.run
    async def run(self, _: WorkflowInput) -> WorkflowOutput:
        return WorkflowOutput()


async def _start_and_execute_workflow_code_for_type_checking_test():
    client = Client(service_client=Mock(spec=ServiceClient))

    # Good
    _handle: WorkflowHandle[TestWorkflow, WorkflowOutput] = await client.start_workflow(
        TestWorkflow.run, WorkflowInput(), id="wid", task_queue="tq"
    )

    # id and task_queue are required
    # TODO: this type error is misleading: it's resolving to an unexpected overload.
    # assert-type-error-pyright: 'Argument of type .+ cannot be assigned to parameter "workflow" of type "str"'
    await client.start_workflow(TestWorkflow.run, id="wid", task_queue="tq")  # type: ignore
    # assert-type-error-pyright: 'No overloads for "start_workflow" match'
    await client.start_workflow(
        TestWorkflow.run,
        # assert-type-error-pyright: 'Argument of type "SignalInput" cannot be assigned to parameter'
        SignalInput(),  # type: ignore
        id="wid",
        task_queue="tq",
    )

    # Good
    _output: WorkflowOutput = await client.execute_workflow(
        TestWorkflow.run, WorkflowInput(), id="wid", task_queue="tq"
    )
    # TODO: this type error is misleading: it's resolving to an unexpected overload.
    # assert-type-error-pyright: 'Argument of type .+ cannot be assigned to parameter "workflow" of type "str"'
    await client.execute_workflow(TestWorkflow.run, id="wid", task_queue="tq")  # type: ignore
    # assert-type-error-pyright: 'No overloads for "execute_workflow" match'
    await client.execute_workflow(
        TestWorkflow.run,
        # assert-type-error-pyright: 'Argument of type "SignalInput" cannot be assigned to parameter'
        SignalInput(),  # type: ignore
        id="wid",
        task_queue="tq",
    )


async def _signal_workflow_code_for_type_checking_test():
    client = Client(service_client=Mock(spec=ServiceClient))
    handle: WorkflowHandle[TestWorkflow, WorkflowOutput] = await client.start_workflow(
        TestWorkflow.run, WorkflowInput(), id="wid", task_queue="tq"
    )

    # Good
    await handle.signal(TestWorkflow.signal, SignalInput())
    # TODO: this type error is misleading: it's resolving to an unexpected overload.
    # assert-type-error-pyright: 'Argument of type .+ cannot be assigned to parameter "signal" of type "str"'
    await handle.signal(TestWorkflow.signal)  # type: ignore

    # TODO: this type error is misleading: it's resolving to an unexpected overload.
    # assert-type-error-pyright: 'Argument of type .+ cannot be assigned to parameter "signal" of type "str"'
    await handle.signal(TestWorkflow2.signal, SignalInput())  # type: ignore


async def _query_workflow_code_for_type_checking_test():
    client = Client(service_client=Mock(spec=ServiceClient))
    handle: WorkflowHandle[TestWorkflow, WorkflowOutput] = await client.start_workflow(
        TestWorkflow.run, WorkflowInput(), id="wid", task_queue="tq"
    )
    # Good
    _: QueryOutput = await handle.query(TestWorkflow.query, QueryInput())
    # TODO: this type error is misleading: it's resolving to an unexpected overload.
    # assert-type-error-pyright: 'Argument of type .+ cannot be assigned to parameter "query" of type "str"'
    await handle.query(TestWorkflow.query)  # type: ignore
    # assert-type-error-pyright: 'Argument of type "SignalInput" cannot be assigned to parameter'
    await handle.query(TestWorkflow.query, SignalInput())  # type: ignore

    # TODO: this type error is misleading: it's resolving to an unexpected overload.
    # assert-type-error-pyright: 'Argument of type .+ cannot be assigned to parameter "query" of type "str"'
    await handle.query(TestWorkflow2.query, QueryInput())  # type: ignore


async def _update_workflow_code_for_type_checking_test():
    client = Client(service_client=Mock(spec=ServiceClient))
    handle: WorkflowHandle[TestWorkflow, WorkflowOutput] = await client.start_workflow(
        TestWorkflow.run, WorkflowInput(), id="wid", task_queue="tq"
    )

    # Good
    _handle: WorkflowUpdateHandle[UpdateOutput] = await handle.start_update(
        TestWorkflow.update, UpdateInput(), wait_for_stage=WorkflowUpdateStage.ACCEPTED
    )
    # wait_for_stage is required
    # assert-type-error-pyright: 'No overloads for "start_update" match'
    await handle.start_update(TestWorkflow.update, UpdateInput())  # type: ignore

    # assert-type-error-pyright: 'No overloads for "start_update" match the provided arguments'
    await handle.start_update(TestWorkflow2.update, UpdateInput())  # type: ignore

    # Good
    _result: UpdateOutput = await handle.execute_update(
        TestWorkflow.update, UpdateInput()
    )
    # assert-type-error-pyright: 'No overloads for "execute_update" match'
    await handle.execute_update(
        TestWorkflow.update,
        wait_for_stage=WorkflowUpdateStage.ACCEPTED,  # type: ignore
    )
    # assert-type-error-pyright: 'Argument of type "SignalInput" cannot be assigned to parameter'
    await handle.execute_update(TestWorkflow.update, SignalInput())  # type: ignore

    # TODO: this type error is misleading: it's resolving to an unecpected overload.
    # assert-type-error-pyright: 'Argument of type .+ cannot be assigned to parameter "update" of type "str"'
    await handle.execute_update(TestWorkflow2.update, UpdateInput())  # type: ignore


async def _update_with_start_workflow_code_for_type_checking_test():
    client = Client(service_client=Mock(spec=ServiceClient))

    # Good
    with_start = WithStartWorkflowOperation(
        TestWorkflow.run,
        WorkflowInput(),
        id="wid",
        task_queue="tq",
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )
    _update_handle: WorkflowUpdateHandle[
        UpdateOutput
    ] = await client.start_update_with_start_workflow(
        TestWorkflow.update,
        UpdateInput(),
        wait_for_stage=WorkflowUpdateStage.ACCEPTED,
        start_workflow_operation=with_start,
    )
    _update_result: UpdateOutput = await _update_handle.result()

    _wf_handle: WorkflowHandle[
        TestWorkflow, WorkflowOutput
    ] = await with_start.workflow_handle()

    _wf_result: WorkflowOutput = await _wf_handle.result()

    # id_conflict_policy is required
    # assert-type-error-pyright: 'No overloads for "__init__" match'
    with_start = WithStartWorkflowOperation(  # type: ignore
        TestWorkflow.run,
        WorkflowInput(),
        id="wid",
        task_queue="tq",
    )

    # wait_for_stage is required
    # assert-type-error-pyright: 'No overloads for "start_update_with_start_workflow" match'
    await client.start_update_with_start_workflow(  # type: ignore
        TestWorkflow.update, UpdateInput(), start_workflow_operation=with_start
    )

    # Good
    _update_result_2: UpdateOutput = await client.execute_update_with_start_workflow(
        TestWorkflow.update,
        UpdateInput(),
        start_workflow_operation=with_start,
    )

    # cannot supply wait_for_stage
    # assert-type-error-pyright: 'No overloads for "execute_update_with_start_workflow" match'
    await client.execute_update_with_start_workflow(  # type: ignore
        TestWorkflow.update,
        UpdateInput(),
        start_workflow_operation=with_start,
        wait_for_stage=WorkflowUpdateStage.ACCEPTED,
    )
