from __future__ import annotations
import asyncio

import dataclasses
import sys
import uuid
from dataclasses import dataclass
from typing import Callable, ClassVar, Dict, List, Optional, Sequence, Type

import pytest

import temporalio.worker.workflow_sandbox
from temporalio import workflow
from temporalio.client import Client, WorkflowFailureError
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
        actual_names == temporalio.worker.workflow_sandbox._stdlib_module_names
    ), f"Expecting names as {actual_names}. In code as:\n{code}"

global_state = ["global orig"]
print("!!GLOBAL STATE!!", id(global_state))

@dataclass
class GlobalStateWorkflowParams:
    fail_on_first_attempt: bool = False


@workflow.defn
class GlobalStateWorkflow:
    def __init__(self) -> None:
        print("!!INIT GLOBAL STATE!!", id(global_state), __import__)
        self.append("inited")

    @workflow.run
    async def run(self, params: GlobalStateWorkflowParams) -> Dict[str, List[str]]:
        print("!!RUN GLOBAL STATE!!", id(global_state))
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
        # SandboxRestrictions.passthrough_modules_maximum,
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
        # print("THIS GLOBAL STATE", global_state)
        assert global_state == ["global orig"]
        assert stateful_module.module_state == ["module orig"]
        print("SUCCESS1!", __import__)


# @dataclass
# class RestrictableObject:
#     foo: Optional[RestrictableObject] = None
#     bar: int = 42
#     baz: ClassVar[int] = 57
#     qux: ClassVar[RestrictableObject]

#     some_dict: Optional[Dict] = None


# RestrictableObject.qux = RestrictableObject(foo=RestrictableObject(bar=70), bar=80)


# def test_workflow_sandbox_restricted_proxy():
#     obj_class = _RestrictedProxy(
#         "RestrictableObject",
#         RestrictableObject,
#         SandboxMatcher(
#             children={
#                 "foo": SandboxMatcher(access={"bar"}),
#                 "qux": SandboxMatcher(children={"foo": SandboxMatcher(access={"foo"})}),
#                 "some_dict": SandboxMatcher(
#                     children={
#                         "key1": SandboxMatcher(access="subkey2"),
#                         "key.2": SandboxMatcher(access="subkey2"),
#                     }
#                 ),
#             }
#         ),
#     )
#     obj = obj_class(
#         foo=obj_class(),
#         some_dict={
#             "key1": {"subkey1": "val", "subkey2": "val"},
#             "key.2": {"subkey1": "val", "subkey2": "val"},
#         },
#     )
#     # Accessing these values is fine
#     _ = (
#         obj.bar,
#         obj.foo.foo,
#         obj_class.baz,
#         obj_class.qux.bar,
#         obj_class.qux.foo.bar,
#         obj.some_dict["key1"]["subkey1"],
#         obj.some_dict["key.2"]["subkey1"],
#     )
#     # But these aren't
#     with pytest.raises(RestrictedWorkflowAccessError) as err:
#         _ = obj.foo.bar
#     assert err.value.qualified_name == "RestrictableObject.foo.bar"
#     with pytest.raises(RestrictedWorkflowAccessError) as err:
#         _ = obj_class.qux.foo.foo.bar
#     assert err.value.qualified_name == "RestrictableObject.qux.foo.foo"
#     with pytest.raises(RestrictedWorkflowAccessError) as err:
#         _ = obj.some_dict["key1"]["subkey2"]
#     assert err.value.qualified_name == "RestrictableObject.some_dict.key1.subkey2"
#     with pytest.raises(RestrictedWorkflowAccessError) as err:
#         _ = obj.some_dict["key.2"]["subkey2"]
#     assert err.value.qualified_name == "RestrictableObject.some_dict.key.2.subkey2"

#     # Unfortunately, we can't intercept the type() call
#     assert type(obj.foo) is _RestrictedProxy


# @workflow.defn
# class RunCodeWorkflow:
#     @workflow.run
#     async def run(self, code: str) -> None:
#         try:
#             exec(code)
#         except RestrictedWorkflowAccessError as err:
#             raise ApplicationError(
#                 str(err), type="RestrictedWorkflowAccessError"
#             ) from err


# async def test_workflow_sandbox_restrictions(client: Client):
#     async with new_worker(client, RunCodeWorkflow) as worker:
#         invalid_code_to_check = [
#             # Restricted module-level callable
#             "import os\nos.getcwd()",
#             # Even if called from import
#             "import os\nfrom os import getcwd\ngetcwd()",
#             # Wildcard "use"
#             "import glob\nglob.glob('whatever')",
#             # General library restrictions
#             "import datetime\ndatetime.date.today()",
#             "import datetime\ndatetime.datetime.now()",
#             "import random\nrandom.choice(['foo', 'bar'])",
#             "import secrets\nsecrets.choice(['foo', 'bar'])",
#             # TODO(cretz): os.path
#         ]
#         for code in invalid_code_to_check:
#             with pytest.raises(WorkflowFailureError) as err:
#                 await client.execute_workflow(
#                     RunCodeWorkflow.run,
#                     code,
#                     id=f"workflow-{uuid.uuid4()}",
#                     task_queue=worker.task_queue,
#                 )
#             assert isinstance(err.value.cause, ApplicationError)
#             assert err.value.cause.type == "RestrictedWorkflowAccessError"

#         valid_code_to_check = [
#             # It is totally ok to reference a callable that you cannot call
#             "import os\n_ = os.getcwd",
#             # Even in an import
#             "import os\nfrom os import getcwd\n_ = os.getcwd",
#             # Can reference wildcard callable that cannot be called
#             "import glob\n_ = glob.glob",
#             # General library allowed calls
#             "import datetime\ndatetime.date(2001, 1, 1)",
#         ]
#         for code in valid_code_to_check:
#             await client.execute_workflow(
#                 RunCodeWorkflow.run,
#                 code,
#                 id=f"workflow-{uuid.uuid4()}",
#                 task_queue=worker.task_queue,
#             )


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
