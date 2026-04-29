"""
This file exists to test for type-checker false positives and false negatives
for workflow helper APIs.

It doesn't contain any test functions - it uses the machinery in
test_type_errors.py to verify that pyright produces the expected errors.
"""

from temporalio import workflow


class WorkflowInput:
    pass


class WorkflowInput2:
    pass


class SignalInput:
    pass


class SignalInput2:
    pass


@workflow.defn
class TestWorkflow:
    @workflow.run
    async def run(self, _: WorkflowInput) -> None:
        pass

    @workflow.signal
    async def signal(self, _: SignalInput) -> None:
        pass


@workflow.defn
class NoArgWorkflow:
    @workflow.run
    async def run(self) -> None:
        pass

    @workflow.signal
    async def signal(self) -> None:
        pass


@workflow.defn
class MultiArgWorkflow:
    @workflow.run
    async def run(self, _: WorkflowInput, _second: WorkflowInput2) -> None:
        pass

    @workflow.signal
    async def signal(self, _: SignalInput, _second: SignalInput2) -> None:
        pass


@workflow.defn
class OtherWorkflow:
    @workflow.run
    async def run(self, _: WorkflowInput) -> None:
        pass

    @workflow.signal
    async def signal(self, _: SignalInput) -> None:
        pass


async def _signal_with_start_workflow_typed_single_arg_happy_path() -> None:  # type:ignore[reportUnusedFunction]
    _handle: workflow.ExternalWorkflowHandle[
        TestWorkflow
    ] = await workflow.signal_with_start_workflow(
        TestWorkflow.run,
        TestWorkflow.signal,
        id="wid",
        workflow_arg=WorkflowInput(),
        signal_arg=SignalInput(),
        task_queue="tq",
    )


async def _signal_with_start_workflow_string_signal_happy_path() -> None:  # type:ignore[reportUnusedFunction]
    _handle: workflow.ExternalWorkflowHandle[
        TestWorkflow
    ] = await workflow.signal_with_start_workflow(
        TestWorkflow.run,
        "signal",
        id="wid",
        workflow_arg=WorkflowInput(),
        signal_arg=SignalInput(),
        task_queue="tq",
    )


async def _signal_with_start_workflow_no_arg_happy_path() -> None:  # type:ignore[reportUnusedFunction]
    _handle: workflow.ExternalWorkflowHandle[
        NoArgWorkflow
    ] = await workflow.signal_with_start_workflow(
        NoArgWorkflow.run,
        NoArgWorkflow.signal,
        id="wid",
        task_queue="tq",
    )


async def _signal_with_start_workflow_args_fallback_happy_path() -> None:  # type:ignore[reportUnusedFunction]
    _handle: workflow.ExternalWorkflowHandle[
        MultiArgWorkflow
    ] = await workflow.signal_with_start_workflow(
        MultiArgWorkflow.run,
        MultiArgWorkflow.signal,
        id="wid",
        workflow_args=[WorkflowInput(), WorkflowInput2()],
        signal_args=[SignalInput(), SignalInput2()],
        task_queue="tq",
    )


async def _signal_with_start_workflow_wrong_workflow_arg() -> None:  # type:ignore[reportUnusedFunction]
    await workflow.signal_with_start_workflow(
        TestWorkflow.run,
        TestWorkflow.signal,
        id="wid",
        # assert-type-error-pyright: 'Argument of type "SignalInput" cannot be assigned to parameter "workflow_arg"'
        workflow_arg=SignalInput(),  # type: ignore
        signal_arg=SignalInput(),
        task_queue="tq",
    )


async def _signal_with_start_workflow_wrong_signal_arg() -> None:  # type:ignore[reportUnusedFunction]
    await workflow.signal_with_start_workflow(
        TestWorkflow.run,
        TestWorkflow.signal,
        id="wid",
        workflow_arg=WorkflowInput(),
        # assert-type-error-pyright: 'Argument of type "WorkflowInput" cannot be assigned to parameter "signal_arg"'
        signal_arg=WorkflowInput(),  # type: ignore
        task_queue="tq",
    )


async def _signal_with_start_workflow_wrong_workflow_arg_with_string_signal() -> None:  # type:ignore[reportUnusedFunction]
    await workflow.signal_with_start_workflow(
        TestWorkflow.run,
        "signal",
        id="wid",
        # assert-type-error-pyright: 'Argument of type "SignalInput" cannot be assigned to parameter "workflow_arg"'
        workflow_arg=SignalInput(),  # type: ignore
        signal_arg=SignalInput(),
        task_queue="tq",
    )


async def _signal_with_start_workflow_multi_arg_workflow_rejects_workflow_arg() -> None:  # type:ignore[reportUnusedFunction]
    await workflow.signal_with_start_workflow(
        # assert-type-error-pyright: 'cannot be assigned to parameter "workflow" of type "str"'
        MultiArgWorkflow.run,  # type: ignore
        MultiArgWorkflow.signal,
        id="wid",
        workflow_arg=WorkflowInput(),  # type: ignore
        signal_args=[SignalInput(), SignalInput2()],
        task_queue="tq",
    )


async def _signal_with_start_workflow_multi_arg_signal_rejects_signal_arg() -> None:  # type:ignore[reportUnusedFunction]
    await workflow.signal_with_start_workflow(
        # assert-type-error-pyright: 'cannot be assigned to parameter "workflow" of type "str"'
        MultiArgWorkflow.run,  # type: ignore
        MultiArgWorkflow.signal,
        id="wid",
        workflow_args=[WorkflowInput(), WorkflowInput2()],
        signal_arg=SignalInput(),  # type: ignore
        task_queue="tq",
    )


async def _ma_workflow_arg_str_sig() -> None:  # type:ignore[reportUnusedFunction]
    await workflow.signal_with_start_workflow(
        # assert-type-error-pyright: 'cannot be assigned to parameter "workflow" of type "str"'
        MultiArgWorkflow.run,  # type: ignore
        "signal",
        id="wid",
        workflow_arg=WorkflowInput(),  # type: ignore
        signal_args=[SignalInput(), SignalInput2()],
        task_queue="tq",
    )


async def _ma_signal_arg_str_sig() -> None:  # type:ignore[reportUnusedFunction]
    await workflow.signal_with_start_workflow(
        # assert-type-error-pyright: 'cannot be assigned to parameter "workflow" of type "str"'
        MultiArgWorkflow.run,  # type: ignore
        "signal",
        id="wid",
        workflow_args=[WorkflowInput(), WorkflowInput2()],
        signal_arg=SignalInput(),  # type: ignore
        task_queue="tq",
    )


async def _signal_with_start_workflow_mismatched_signal_callable() -> None:  # type:ignore[reportUnusedFunction]
    # assert-type-error-pyright: 'No overloads for "signal_with_start_workflow" match'
    await workflow.signal_with_start_workflow(
        TestWorkflow.run,
        OtherWorkflow.signal,  # type: ignore
        id="wid",
        workflow_arg=WorkflowInput(),
        signal_arg=SignalInput(),
        task_queue="tq",
    )
