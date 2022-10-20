from __future__ import annotations

import dataclasses
import functools
import os
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Type

import pytest

import temporalio.worker.workflow_sandbox.restrictions
from temporalio import workflow
from temporalio.client import Client, WorkflowFailureError, WorkflowHandle
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    RestrictedWorkflowAccessError,
    SandboxedWorkflowRunner,
    SandboxMatcher,
    SandboxRestrictions,
)
from tests.worker.test_workflow import assert_eq_eventually
from tests.worker.workflow_sandbox.testmodules import stateful_module


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
        actual_names
        == temporalio.worker.workflow_sandbox.restrictions._stdlib_module_names
    ), f"Expecting names as {actual_names}. In code as:\n{code}"


global_state = ["global orig"]
# We just access os.name in here to show we _can_. It's access-restricted at
# runtime only
_ = os.name


@dataclass
class GlobalStateWorkflowParams:
    fail_on_first_attempt: bool = False


@workflow.defn
class GlobalStateWorkflow:
    def __init__(self) -> None:
        # We sleep here to defer to other threads so we can test the
        # thread-safety of global state isolation
        time.sleep(0)
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
        # TODO(cretz): See what is failing here
        SandboxRestrictions.passthrough_modules_with_temporal,
        SandboxRestrictions.passthrough_modules_maximum,
    ],
)
async def test_workflow_sandbox_global_state(
    client: Client,
    sandboxed_passthrough_modules: SandboxMatcher,
):
    global global_state
    async with new_worker(
        client,
        GlobalStateWorkflow,
        sandboxed_passthrough_modules=sandboxed_passthrough_modules,
    ) as worker:
        # Start several workflows in the sandbox and make sure none of it
        # clashes
        handles: List[WorkflowHandle] = []
        for _ in range(10):
            handles.append(
                await client.start_workflow(
                    GlobalStateWorkflow.run,
                    GlobalStateWorkflowParams(),
                    id=f"workflow-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                )
            )

        # Wait until every workflow started
        async def handle_started(handle: WorkflowHandle) -> bool:
            return (
                "started" in (await handle.query(GlobalStateWorkflow.state))["global"]
            )

        for handle in handles:
            await assert_eq_eventually(True, functools.partial(handle_started, handle))

        # Send a signal to each one, but specific to its index
        for i, handle in enumerate(handles):
            await handle.signal(GlobalStateWorkflow.append, f"signal{i}")

        # Tell each one to finish
        for handle in handles:
            await handle.signal(GlobalStateWorkflow.append, "finish")

        # Get result of each and confirm state is as expected
        for i, handle in enumerate(handles):
            assert {
                "global": ["global orig", "inited", "started", f"signal{i}", "finish"],
                "module": ["module orig", "inited", "started", f"signal{i}", "finish"],
            } == await handle.result()

        # Confirm that _this_ global state wasn't even touched
        assert global_state == ["global orig"]
        assert stateful_module.module_state == ["module orig"]


@workflow.defn
class RunCodeWorkflow:
    @workflow.run
    async def run(self, code: str) -> None:
        try:
            exec(code)
        except RestrictedWorkflowAccessError as err:
            raise ApplicationError(
                str(err), type="RestrictedWorkflowAccessError"
            ) from err


async def test_workflow_sandbox_restrictions(client: Client):
    async with new_worker(client, RunCodeWorkflow) as worker:
        invalid_code_to_check = [
            # Restricted module-level callable
            "import os\nos.getcwd()",
            # Even if called from import
            "import os\nfrom os import getcwd\ngetcwd()",
            # Wildcard "use"
            "import glob\nglob.glob('whatever')",
            # Runtime-only access restriction on var
            "import os\n_ = os.name",
            # Builtin
            "open('somefile')",
            # General library restrictions
            "import datetime\ndatetime.date.today()",
            "import datetime\ndatetime.datetime.now()",
            "import random\nrandom.choice(['foo', 'bar'])",
            "import secrets\nsecrets.choice(['foo', 'bar'])",
            # TODO(cretz): os.path
        ]
        for code in invalid_code_to_check:
            with pytest.raises(WorkflowFailureError) as err:
                await client.execute_workflow(
                    RunCodeWorkflow.run,
                    code,
                    id=f"workflow-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                )
            assert isinstance(err.value.cause, ApplicationError)
            assert err.value.cause.type == "RestrictedWorkflowAccessError"

        valid_code_to_check = [
            # It is totally ok to reference a callable that you cannot call
            "import os\n_ = os.getcwd",
            # Even in an import
            "import os\nfrom os import getcwd\n_ = os.getcwd",
            # Can reference wildcard callable that cannot be called
            "import glob\n_ = glob.glob",
            # General library allowed calls
            "import datetime\ndatetime.date(2001, 1, 1)",
        ]
        for code in valid_code_to_check:
            await client.execute_workflow(
                RunCodeWorkflow.run,
                code,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )


def new_worker(
    client: Client,
    *workflows: Type,
    activities: Sequence[Callable] = [],
    task_queue: Optional[str] = None,
    sandboxed_passthrough_modules: Optional[SandboxMatcher] = None,
    sandboxed_invalid_module_members: Optional[SandboxMatcher] = None,
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
        workflow_runner=SandboxedWorkflowRunner(restrictions=restrictions),
    )
