import dataclasses
import sys
import uuid
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Type

import pytest

import temporalio.worker.workflow_sandbox
from temporalio import workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.worker import (
    SandboxedWorkflowRunner,
    SandboxPattern,
    SandboxRestrictions,
    UnsandboxedWorkflowRunner,
    Worker,
)
from tests.worker import stateful_module
from tests.worker.test_workflow import assert_eq_eventually


def test_workflow_sandbox_stdlib_module_names():
    if sys.version_info < (3, 10):
        pytest.skip("Test only runs on 3.10")
    actual_names = ",".join(sorted(sys.stdlib_module_names))
    # Uncomment to print code for generating these
    code_lines = [""]
    for mod_name in sorted(sys.stdlib_module_names):
        if code_lines[-1]:
            code_lines[-1] += ","
        if len(code_lines[-1]) > 80:
            code_lines.append("")
        code_lines[-1] += mod_name
    code = f'_stdlib_module_names = (\n    "' + '"\n    "'.join(code_lines) + '"\n)'
    # TODO(cretz): Point releases may add modules :-(
    assert (
        actual_names == temporalio.worker.workflow_sandbox._stdlib_module_names
    ), f"Expecting names as {actual_names}. In code as:\n{code}"


global_state = ["global orig"]


@dataclass
class GlobalStateWorkflowParams:
    fail_on_first_attempt: bool = False


@workflow.defn
class GlobalStateWorkflow:
    def __init__(self) -> None:
        self.append("inited")

    @workflow.run
    async def run(self, params: GlobalStateWorkflowParams) -> Dict[str, List[str]]:
        self.append("started")
        if params.fail_on_first_attempt:
            raise ApplicationError("Failing first attempt")
        # Wait for "finish" to be in the state
        await workflow.wait_condition(lambda: "finish" in global_state)
        return self.state()

    @workflow.signal
    def append(self, str: str) -> None:
        global_state.append(str)
        stateful_module.module_state.append(str)

    @workflow.query
    def state(self) -> Dict[str, List[str]]:
        return {"global": global_state, "module": stateful_module.module_state}


@pytest.mark.parametrize(
    "sandboxed_passthrough_modules",
    [
        # TODO(cretz): Disabling until https://github.com/protocolbuffers/upb/pull/804
        # SandboxRestrictions.passthrough_modules_minimum,
        SandboxRestrictions.passthrough_modules_with_temporal,
        SandboxRestrictions.passthrough_modules_maximum,
    ],
)
async def test_workflow_sandbox_global_state(
    client: Client,
    sandboxed_passthrough_modules: SandboxPattern,
):
    async with new_worker(
        client,
        GlobalStateWorkflow,
        sandboxed_passthrough_modules=sandboxed_passthrough_modules,
    ) as worker:
        # Run it twice sandboxed and check that the results don't affect each
        # other
        handle1 = await client.start_workflow(
            GlobalStateWorkflow.run,
            GlobalStateWorkflowParams(),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        handle2 = await client.start_workflow(
            GlobalStateWorkflow.run,
            GlobalStateWorkflowParams(),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        # Wait until both started so our signals are _after_ start
        async def handle1_started() -> bool:
            return (
                "started" in (await handle1.query(GlobalStateWorkflow.state))["global"]
            )

        async def handle2_started() -> bool:
            return (
                "started" in (await handle1.query(GlobalStateWorkflow.state))["global"]
            )

        await assert_eq_eventually(True, handle1_started)
        await assert_eq_eventually(True, handle2_started)

        await handle1.signal(GlobalStateWorkflow.append, "signal1")
        await handle2.signal(GlobalStateWorkflow.append, "signal2")

        await handle1.signal(GlobalStateWorkflow.append, "finish")
        await handle2.signal(GlobalStateWorkflow.append, "finish")

        # Confirm state is as expected
        assert {
            "global": ["global orig", "inited", "started", "signal1", "finish"],
            "module": ["module orig", "inited", "started", "signal1", "finish"],
        } == await handle1.result()
        assert {
            "global": ["global orig", "inited", "started", "signal2", "finish"],
            "module": ["module orig", "inited", "started", "signal2", "finish"],
        } == await handle2.result()

        # But that _this_ global state wasn't even touched
        assert global_state == ["global orig"]
        assert stateful_module.module_state == ["module orig"]


# TODO(cretz): To test:
# * Invalid modules
# * Invalid module members (all forms of access/use)
# * Different preconfigured passthroughs
# * Import from with "as" that is restricted
# * Restricted modules that are still passed through


@workflow.defn
class InvalidMemberWorkflow:
    @workflow.run
    async def run(self, action: str) -> None:
        try:
            if action == "get_bad_global_var":
                workflow.logger.info(stateful_module.bad_global_var)
        except temporalio.worker.workflow_sandbox.RestrictedWorkflowAccessError as err:
            raise ApplicationError(
                f"Got restriction", type="RestrictedWorkflowAccessError"
            ) from err


async def test_workflow_sandbox_restrictions(client: Client):
    # TODO(cretz): Var read, var write, class create, class static access, func calls, class methods, class creation
    async with new_worker(
        client,
        InvalidMemberWorkflow,
        sandboxed_invalid_module_members=SandboxPattern.from_dotted_strs(
            [
                "tests.worker.stateful_module.bad_global_var",
            ]
        ),
    ) as worker:

        async def assert_restriction(action: str) -> None:
            with pytest.raises(WorkflowFailureError) as err:
                await client.execute_workflow(
                    InvalidMemberWorkflow.run,
                    action,
                    id=f"workflow-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                )
            assert isinstance(err.value.cause, ApplicationError)
            assert err.value.cause.type == "RestrictedWorkflowAccessError"

        await assert_restriction("get_bad_global_var")


def new_worker(
    client: Client,
    *workflows: Type,
    activities: Sequence[Callable] = [],
    task_queue: Optional[str] = None,
    sandboxed: bool = True,
    sandboxed_passthrough_modules: Optional[SandboxPattern] = None,
    sandboxed_invalid_module_members: Optional[SandboxPattern] = None,
) -> Worker:
    restrictions = SandboxRestrictions.default
    if sandboxed_passthrough_modules:
        restrictions = dataclasses.replace(
            restrictions, passthrough_modules=sandboxed_passthrough_modules
        )
    if sandboxed_invalid_module_members:
        restrictions = dataclasses.replace(
            restrictions, invalid_module_members=sandboxed_invalid_module_members
        )
    return Worker(
        client,
        task_queue=task_queue or str(uuid.uuid4()),
        workflows=workflows,
        activities=activities,
        workflow_runner=SandboxedWorkflowRunner(restrictions=restrictions)
        if sandboxed
        else UnsandboxedWorkflowRunner(),
    )
