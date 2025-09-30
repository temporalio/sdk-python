"""Test that CommandAwarePayloadVisitor handles all commands with seq fields that have payloads."""

from temporalio.bridge._visitor import PayloadVisitor
from temporalio.worker._command_aware_visitor import (
    CommandAwarePayloadVisitor,
    _get_workflow_activation_job_protos_with_seq,
    _get_workflow_command_protos_with_seq,
)


def test_command_aware_visitor_has_methods_for_all_seq_protos_with_payloads():
    """Verify CommandAwarePayloadVisitor has methods for all protos with seq fields that have payloads.

    We only override methods when the base class has a visitor method (i.e., there are payloads to visit).
    Commands without payloads don't need overrides since there's nothing to visit.
    """
    visitor = CommandAwarePayloadVisitor()

    # Find all protos with seq
    command_protos = list(_get_workflow_command_protos_with_seq())
    job_protos = list(_get_workflow_activation_job_protos_with_seq())
    assert command_protos, "Should find workflow commands with seq"
    assert job_protos, "Should find workflow activation jobs with seq"

    # Check workflow commands - only ones with payloads need overrides
    commands_missing = []
    commands_with_payloads = []
    for proto_class in command_protos:
        method_name = f"_visit_coresdk_workflow_commands_{proto_class.__name__}"
        # Only check if base class has this visitor (meaning there are payloads)
        if hasattr(PayloadVisitor, method_name):
            commands_with_payloads.append(proto_class.__name__)
            # Check if CommandAwarePayloadVisitor has its own override (not just inherited)
            if method_name not in CommandAwarePayloadVisitor.__dict__:
                commands_missing.append(proto_class.__name__)

    # Check workflow activation jobs - only ones with payloads need overrides
    jobs_missing = []
    jobs_with_payloads = []
    for proto_class in job_protos:
        method_name = f"_visit_coresdk_workflow_activation_{proto_class.__name__}"
        # Only check if base class has this visitor (meaning there are payloads)
        if hasattr(PayloadVisitor, method_name):
            jobs_with_payloads.append(proto_class.__name__)
            # Check if CommandAwarePayloadVisitor has its own override (not just inherited)
            if method_name not in CommandAwarePayloadVisitor.__dict__:
                jobs_missing.append(proto_class.__name__)

    errors = []
    if commands_missing:
        errors.append(
            f"Missing visitor methods for commands with seq and payloads: {commands_missing}\n"
            f"Add methods to CommandAwarePayloadVisitor for these commands."
        )
    if jobs_missing:
        errors.append(
            f"Missing visitor methods for activation jobs with seq and payloads: {jobs_missing}\n"
            f"Add methods to CommandAwarePayloadVisitor for these jobs."
        )

    assert not errors, "\n".join(errors)

    # Verify we found the expected commands/jobs with payloads
    assert len(commands_with_payloads) > 0, "Should find commands with payloads"
    assert len(jobs_with_payloads) > 0, "Should find activation jobs with payloads"

    # Sanity check: we should have fewer overrides than total protos with seq
    # (because some don't have payloads)
    assert len(commands_with_payloads) < len(
        command_protos
    ), "Should have some commands without payloads"
    # All activation jobs except FireTimer have payloads
    assert (
        len(jobs_with_payloads) == len(job_protos) - 1
    ), "Should have exactly one activation job without payloads (FireTimer)"
