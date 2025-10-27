from temporalio.worker.workflow_sandbox._restrictions import (
    SandboxMatcher,
    SandboxRestrictions,
)
from temporalio.workflow import SandboxImportNotificationPolicy

restrictions = SandboxRestrictions(
    passthrough_modules=SandboxRestrictions.passthrough_modules_with_temporal
    | {"tests.worker.workflow_sandbox.testmodules.passthrough_module"},
    invalid_modules=SandboxMatcher.nested_child(
        "tests.worker.workflow_sandbox.testmodules".split("."),
        SandboxMatcher(access={"invalid_module"}),
    ),
    invalid_module_members=SandboxMatcher.nested_child(
        "tests.worker.workflow_sandbox.testmodules.invalid_module_members".split("."),
        SandboxMatcher(use={"invalid_function"}),
    ),
    import_notification_policy=SandboxImportNotificationPolicy.WARN_ON_DYNAMIC_IMPORT,
)
