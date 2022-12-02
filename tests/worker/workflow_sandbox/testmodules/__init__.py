from temporalio.worker.workflow_sandbox._restrictions import (
    SandboxMatcher,
    SandboxRestrictions,
)

restrictions = SandboxRestrictions(
    passthrough_modules=SandboxRestrictions.passthrough_modules_minimum
    | {"tests.worker.workflow_sandbox.testmodules.passthrough_module"},
    invalid_modules=SandboxMatcher.nested_child(
        "tests.worker.workflow_sandbox.testmodules".split("."),
        SandboxMatcher(access={"invalid_module"}),
    ),
    invalid_module_members=SandboxMatcher.nested_child(
        "tests.worker.workflow_sandbox.testmodules.invalid_module_members".split("."),
        SandboxMatcher(use={"invalid_function"}),
    ),
)
