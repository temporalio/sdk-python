from __future__ import annotations

import dataclasses
import functools
import inspect
import os
import time
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from enum import IntEnum
from typing import Callable, Dict, List, Optional, Sequence, Type

import pytest

import temporalio.worker.workflow_sandbox._restrictions
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError, WorkflowHandle
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    RestrictedWorkflowAccessError,
    SandboxedWorkflowRunner,
    SandboxMatcher,
    SandboxRestrictions,
)
from tests.helpers import assert_eq_eventually
from tests.worker.workflow_sandbox.testmodules import stateful_module

global_state = ["global orig"]
# We just access os.name in here to show we _can_. It's access-restricted at
# runtime only
_ = os.name

# TODO(cretz): This fails because you can't subclass a restricted class
# class MyZipFile(zipfile.ZipFile):
#     pass


@dataclass
class GlobalStateWorkflowParams:
    fail_on_first_attempt: bool = False


@workflow.defn
class GlobalStateWorkflow:
    def __init__(self) -> None:
        # We sleep here to defer to other threads so we can test the
        # thread-safety of global state isolation
        with workflow.unsafe.sandbox_unrestricted():
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
        # Check restricted stuff
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
            # Importing module calling bad thing
            "import tests.worker.workflow_sandbox.testmodules.module_calling_invalid",
            # General library restrictions
            "import datetime\ndatetime.date.today()",
            "import datetime\ndatetime.datetime.now()",
            "import os\ngetattr(os.environ, 'foo')",
            "import os\nos.getenv('foo')",
            "import os.path\nos.path.abspath('foo')",
            "import random\nrandom.choice(['foo', 'bar'])",
            "import secrets\nsecrets.choice(['foo', 'bar'])",
            "import threading\nthreading.current_thread()",
            "import time\ntime.sleep(1)",
            "import concurrent.futures\nconcurrent.futures.ThreadPoolExecutor()",
            "import urllib.request\nurllib.request.urlopen('example.com')",
            "import http.client\nhttp.client.HTTPConnection('example.com')",
            "import uuid\nuuid.uuid4()",
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

        # Check unrestricted stuff
        valid_code_to_check = [
            # It is totally ok to reference a callable that you cannot call
            "import os\n_ = os.getcwd",
            # Even in an import
            "import os\nfrom os import getcwd\n_ = os.getcwd",
            # Can reference wildcard callable that cannot be called
            "import glob\n_ = glob.glob",
            # Allowed to call random choice when restrictions are disabled
            "import random\n"
            "import temporalio.workflow\n"
            "with temporalio.workflow.unsafe.sandbox_unrestricted():\n"
            "    random.choice(['foo', 'bar'])",
            # Allowed to import bad module when restrictions are disabled
            "import temporalio.workflow\n"
            "with temporalio.workflow.unsafe.sandbox_unrestricted():\n"
            "    import tests.worker.workflow_sandbox.testmodules.module_calling_invalid",
            # General library allowed calls
            "import datetime\ndatetime.date(2001, 1, 1)",
            "import uuid\nuuid.uuid5(uuid.NAMESPACE_DNS, 'example.com')",
        ]
        for code in valid_code_to_check:
            await client.execute_workflow(
                RunCodeWorkflow.run,
                code,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

    # Confirm it's easy to unrestrict stuff
    async with new_worker(
        client,
        RunCodeWorkflow,
        sandboxed_invalid_module_members=SandboxRestrictions.invalid_module_members_default.with_child_unrestricted(
            "datetime", "date", "today"
        ),
    ) as worker:
        await client.execute_workflow(
            RunCodeWorkflow.run,
            "import datetime\ndatetime.date.today()",
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

    # Confirm it's easy to add restrictions
    async with new_worker(
        client,
        RunCodeWorkflow,
        sandboxed_invalid_module_members=SandboxRestrictions.invalid_module_members_default
        | SandboxMatcher(children={"datetime": SandboxMatcher(use={"date"})}),
    ) as worker:
        with pytest.raises(WorkflowFailureError) as err:
            await client.execute_workflow(
                RunCodeWorkflow.run,
                "import datetime\ndatetime.date(2001, 1, 1)",
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
        assert isinstance(err.value.cause, ApplicationError)
        assert err.value.cause.type == "RestrictedWorkflowAccessError"


@workflow.defn
class DateOperatorWorkflow:
    @workflow.run
    async def run(self) -> int:
        assert (
            type(date(2010, 1, 20))
            == temporalio.worker.workflow_sandbox._restrictions._RestrictedProxy
        )
        return (date(2010, 1, 20) - date(2010, 1, 1)).days


async def test_workflow_sandbox_operator(client: Client):
    # Just confirm a simpler operator works
    async with new_worker(client, DateOperatorWorkflow) as worker:
        assert 19 == await client.execute_workflow(
            DateOperatorWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )


global_in_sandbox = workflow.unsafe.in_sandbox()


@workflow.defn
class InSandboxWorkflow:
    def __init__(self) -> None:
        assert global_in_sandbox
        assert workflow.unsafe.in_sandbox()

    @workflow.run
    async def run(self) -> None:
        assert global_in_sandbox
        assert workflow.unsafe.in_sandbox()


async def test_workflow_sandbox_assert(client: Client):
    async with new_worker(client, InSandboxWorkflow) as worker:
        assert not global_in_sandbox
        assert not workflow.unsafe.in_sandbox()
        await client.execute_workflow(
            InSandboxWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )


@workflow.defn
class AccessStackWorkflow:
    @workflow.run
    async def run(self) -> str:
        return inspect.stack()[0].function


async def test_workflow_sandbox_access_stack(client: Client):
    async with new_worker(client, AccessStackWorkflow) as worker:
        assert "run" == await client.execute_workflow(
            AccessStackWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )


class InstanceCheckEnum(IntEnum):
    FOO = 1
    BAR = 2


@dataclass
class InstanceCheckData:
    some_enum: InstanceCheckEnum


@activity.defn
async def instance_check_activity(param: InstanceCheckData) -> InstanceCheckData:
    assert isinstance(param, InstanceCheckData)
    assert param.some_enum is InstanceCheckEnum.BAR
    return param


@workflow.defn
class InstanceCheckWorkflow:
    @workflow.run
    async def run(self, param: InstanceCheckData) -> InstanceCheckData:
        assert isinstance(param, InstanceCheckData)
        assert param.some_enum is InstanceCheckEnum.BAR
        # Exec child if not a child, otherwise exec activity
        if workflow.info().parent is None:
            return await workflow.execute_child_workflow(
                InstanceCheckWorkflow.run, param
            )
        return await workflow.execute_activity(
            instance_check_activity,
            param,
            schedule_to_close_timeout=timedelta(minutes=1),
        )


async def test_workflow_sandbox_instance_check(client: Client):
    async with new_worker(
        client, InstanceCheckWorkflow, activities=[instance_check_activity]
    ) as worker:
        await client.execute_workflow(
            InstanceCheckWorkflow.run,
            InstanceCheckData(some_enum=InstanceCheckEnum.BAR),
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
