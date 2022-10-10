from __future__ import annotations

import dataclasses
import os
import sys
import uuid
from dataclasses import dataclass
from typing import Callable, ClassVar, Dict, List, Optional, Sequence, Type

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
from temporalio.worker.workflow_sandbox import (
    RestrictedWorkflowAccessError,
    _RestrictedProxy,
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


def test_workflow_sandbox_pattern_from_value():
    actual = SandboxPattern.from_value(
        [
            "foo.bar",
            "qux.foo.foo",
            {
                "bar": "baz",
                "baz": True,
                "foo": {"sub1": ["sub2.sub3"]},
            },
        ]
    )
    assert actual == SandboxPattern(
        children={
            "foo": SandboxPattern(
                children={
                    "bar": SandboxPattern.all,
                    "sub1": SandboxPattern(
                        children={
                            "sub2": SandboxPattern(
                                children={"sub3": SandboxPattern.all}
                            ),
                        }
                    ),
                }
            ),
            "bar": SandboxPattern(children={"baz": SandboxPattern.all}),
            "baz": SandboxPattern.all,
            "qux": SandboxPattern(
                children={"foo": SandboxPattern(children={"foo": SandboxPattern.all})}
            ),
        }
    )


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


@dataclass
class RestrictableObject:
    foo: Optional[RestrictableObject] = None
    bar: int = 42
    baz: ClassVar[int] = 57
    qux: ClassVar[RestrictableObject]

    some_dict: Optional[Dict] = None


RestrictableObject.qux = RestrictableObject(foo=RestrictableObject(bar=70), bar=80)


def test_workflow_sandbox_restricted_proxy():
    obj_class = _RestrictedProxy(
        "RestrictableObject",
        RestrictableObject,
        SandboxPattern.from_value(
            [
                "foo.bar",
                "qux.foo.foo",
                "some_dict.key1.subkey2",
                "some_dict.key.2.subkey1",
                "some_dict.key\\.2.subkey2",
            ]
        ),
    )
    obj = obj_class(
        foo=obj_class(),
        some_dict={
            "key1": {"subkey1": "val", "subkey2": "val"},
            "key.2": {"subkey1": "val", "subkey2": "val"},
        },
    )
    # Accessing these values is fine
    _ = (
        obj.bar,
        obj.foo.foo,
        obj_class.baz,
        obj_class.qux.bar,
        obj_class.qux.foo.bar,
        obj.some_dict["key1"]["subkey1"],
        obj.some_dict["key.2"]["subkey1"],
    )
    # But these aren't
    with pytest.raises(RestrictedWorkflowAccessError) as err:
        _ = obj.foo.bar
    assert err.value.qualified_name == "RestrictableObject.foo.bar"
    with pytest.raises(RestrictedWorkflowAccessError) as err:
        _ = obj_class.qux.foo.foo.bar
    assert err.value.qualified_name == "RestrictableObject.qux.foo.foo"
    with pytest.raises(RestrictedWorkflowAccessError) as err:
        _ = obj.some_dict["key1"]["subkey2"]
    assert err.value.qualified_name == "RestrictableObject.some_dict.key1.subkey2"
    with pytest.raises(RestrictedWorkflowAccessError) as err:
        _ = obj.some_dict["key.2"]["subkey2"]
    assert err.value.qualified_name == "RestrictableObject.some_dict.key.2.subkey2"


# TODO(cretz): To test:
# * Invalid modules
# * Invalid module members (all forms of access/use)
# * Different preconfigured passthroughs
# * Import from with "as" that is restricted
# * Restricted modules that are still passed through
# * Import w/ bad top-level code


@workflow.defn
class InvalidMemberWorkflow:
    @workflow.run
    async def run(self, action: str) -> None:
        try:
            if action == "get_bad_module_var":
                workflow.logger.info(os.name)
            elif action == "set_bad_module_var":
                os.name = "bad"
            elif action == "call_bad_module_func":
                os.getcwd()
            elif action == "call_bad_builtin_func":
                open("does-not-exist.txt")
        except RestrictedWorkflowAccessError as err:
            raise ApplicationError(
                str(err), type="RestrictedWorkflowAccessError"
            ) from err


async def test_workflow_sandbox_restrictions(client: Client):
    # TODO(cretz): Things to Var read, var write, class create, class static access, func calls, class methods, class creation

    async with new_worker(client, InvalidMemberWorkflow) as worker:

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

        actions_to_check = [
            "get_bad_module_var",
            "set_bad_module_var",
            "call_bad_module_func",
            "call_bad_builtin_func",
            # TODO(cretz): How can we prevent this while also letting the stdlib
            # to its own setattr?
            # "set_module_var_on_passthrough",
            # "delete_module_var_on_passthrough",
            # TODO(cretz):
            # "mutate_bad_sequence"
            # "create_bad_class"
        ]
        for action in actions_to_check:
            await assert_restriction(action)


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
