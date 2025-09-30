"""Test that CommandAwarePayloadVisitor handles all commands with seq fields."""

from temporalio.worker._command_aware_visitor import (
    CommandAwarePayloadVisitor,
    _get_workflow_activation_job_protos_with_seq,
    _get_workflow_command_protos_with_seq,
)


def test_command_aware_visitor_has_methods_for_all_seq_protos():
    """Verify CommandAwarePayloadVisitor has methods for all protos with seq fields."""
    visitor = CommandAwarePayloadVisitor()

    # Check all workflow commands with seq have corresponding methods

    command_protos = list(_get_workflow_command_protos_with_seq())
    job_protos = list(_get_workflow_activation_job_protos_with_seq())
    assert command_protos, "Should find workflow commands with seq"
    assert job_protos, "Should find workflow activation jobs with seq"

    commands_missing = []
    for proto_class in command_protos:
        method_name = f"_visit_coresdk_workflow_commands_{proto_class.__name__}"
        if not hasattr(visitor, method_name):
            commands_missing.append(proto_class.__name__)

    # Check all workflow activation jobs with seq have corresponding methods
    jobs_missing = []
    for proto_class in job_protos:
        method_name = f"_visit_coresdk_workflow_activation_{proto_class.__name__}"
        if not hasattr(visitor, method_name):
            jobs_missing.append(proto_class.__name__)

    errors = []
    if commands_missing:
        errors.append(
            f"Missing visitor methods for commands with seq: {commands_missing}\n"
            f"Add methods to CommandAwarePayloadVisitor for these commands."
        )
    if jobs_missing:
        errors.append(
            f"Missing visitor methods for activation jobs with seq: {jobs_missing}\n"
            f"Add methods to CommandAwarePayloadVisitor for these jobs."
        )

    assert not errors, "\n".join(errors)
