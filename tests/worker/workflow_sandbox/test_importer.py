import pytest

from temporalio.worker.workflow_sandbox._importer import Importer
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

    # Now import both via importer
    with Importer(restrictions, RestrictionContext()).applied():
        import tests.worker.workflow_sandbox.testmodules.passthrough_module as inside1
        import tests.worker.workflow_sandbox.testmodules.stateful_module as inside2

    # Now if we alter inside1, it's passthrough so it affects outside1
    inside1.module_state = ["another val"]
    assert outside1.module_state == ["another val"]
    assert id(inside1) == id(outside1)

    # But if we alter non-passthrough inside2 it does not affect outside2
    inside2.module_state = ["another val"]
    assert outside2.module_state != ["another val"]
    assert id(inside2) != id(outside2)


def test_workflow_sandbox_importer_invalid_module_members():
    importer = Importer(restrictions, RestrictionContext())
    # Can access the function, no problem
    with importer.applied():
        import tests.worker.workflow_sandbox.testmodules.invalid_module_members

        _ = (
            tests.worker.workflow_sandbox.testmodules.invalid_module_members.invalid_function
        )

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
