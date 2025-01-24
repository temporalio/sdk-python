import sys

import pytest

from temporalio import workflow
from temporalio.worker.workflow_sandbox._importer import (
    Importer,
    _thread_local_sys_modules,
    _ThreadLocalSysModules,
)
from temporalio.worker.workflow_sandbox._restrictions import (
    RestrictedWorkflowAccessError,
    RestrictionContext,
)

from .testmodules import restrictions


def test_workflow_sandbox_importer_invalid_module():
    with pytest.raises(RestrictedWorkflowAccessError) as err:
        with Importer(restrictions, RestrictionContext()).applied():
            import tests.worker.workflow_sandbox.testmodules.invalid_module
    assert (
        err.value.qualified_name
        == "tests.worker.workflow_sandbox.testmodules.invalid_module"
    )


def test_workflow_sandbox_importer_passthrough_module():
    # Import outside of importer
    import tests.worker.workflow_sandbox.testmodules.passthrough_module as outside1
    import tests.worker.workflow_sandbox.testmodules.stateful_module as outside2

    assert outside1.module_state == ["module orig"]
    assert outside2.module_state == ["module orig"]

    # Now import via importer
    with Importer(restrictions, RestrictionContext()).applied():
        import tests.worker.workflow_sandbox.testmodules.passthrough_module as inside1
        import tests.worker.workflow_sandbox.testmodules.stateful_module as inside2

        from .testmodules import stateful_module as inside_relative2

    # Now if we alter inside1, it's passthrough so it affects outside1
    inside1.module_state = ["another val"]
    assert outside1.module_state == ["another val"]
    assert id(inside1) == id(outside1)

    # Confirm relative is same as non-relative
    assert id(inside2) == id(inside_relative2)

    # But if we alter non-passthrough inside2 it does not affect outside2
    inside2.module_state = ["another val"]
    assert outside2.module_state != ["another val"]
    assert id(inside2) != id(outside2)


def test_workflow_sandbox_importer_passthough_context_manager():
    import tests.worker.workflow_sandbox.testmodules.stateful_module as outside

    with Importer(restrictions, RestrictionContext()).applied():
        with workflow.unsafe.imports_passed_through():
            import tests.worker.workflow_sandbox.testmodules.stateful_module as inside
    assert id(outside) == id(inside)


def test_workflow_sandbox_importer_passthrough_all_modules():
    import tests.worker.workflow_sandbox.testmodules.stateful_module as outside

    # Confirm regular restrictions does re-import
    with Importer(restrictions, RestrictionContext()).applied():
        import tests.worker.workflow_sandbox.testmodules.stateful_module as inside1
    assert id(outside) != id(inside1)

    # But that one with all modules passed through does not
    with Importer(
        restrictions.with_passthrough_all_modules(), RestrictionContext()
    ).applied():
        import tests.worker.workflow_sandbox.testmodules.stateful_module as inside2
    assert id(outside) == id(inside2)


def test_workflow_sandbox_importer_invalid_module_members():
    importer = Importer(restrictions, RestrictionContext())
    # Can access the function, no problem
    with importer.applied():
        import tests.worker.workflow_sandbox.testmodules.invalid_module_members

        _ = tests.worker.workflow_sandbox.testmodules.invalid_module_members.invalid_function

    # Cannot call qualified
    with pytest.raises(RestrictedWorkflowAccessError) as err:
        with importer.applied():
            import tests.worker.workflow_sandbox.testmodules.invalid_module_members

            tests.worker.workflow_sandbox.testmodules.invalid_module_members.invalid_function()
    assert (
        err.value.qualified_name
        == "tests.worker.workflow_sandbox.testmodules.invalid_module_members.invalid_function.__call__"
    )

    # Cannot call via from import either
    with pytest.raises(RestrictedWorkflowAccessError) as err:
        with importer.applied():
            from tests.worker.workflow_sandbox.testmodules.invalid_module_members import (
                invalid_function,
            )

            invalid_function()
    assert (
        err.value.qualified_name
        == "tests.worker.workflow_sandbox.testmodules.invalid_module_members.invalid_function.__call__"
    )


def test_thread_local_sys_module_attrs():
    if sys.version_info < (3, 9):
        pytest.skip("Dict or methods only in >= 3.9")
    # Python chose not to put everything in MutableMapping they do in dict, see
    # https://bugs.python.org/issue22101. Therefore we manually confirm that
    # every attribute of sys modules is also in thread local sys modules to
    # ensure compatibility.
    for attr in dir(sys.modules):
        getattr(_thread_local_sys_modules, attr)

    # Let's also test "or" and "copy"
    norm = {"foo": 123}
    thread_local = _ThreadLocalSysModules({"foo": 123})  # type: ignore[dict-item]
    assert (norm | {"bar": 456}) == (thread_local | {"bar": 456})
    norm |= {"baz": 789}
    thread_local |= {"baz": 789}  # type: ignore
    assert norm.copy() == thread_local.copy()
