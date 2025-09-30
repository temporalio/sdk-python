"""Visitor that sets command context during payload traversal."""

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Optional, Type

from temporalio.api.enums.v1.command_type_pb2 import CommandType
from temporalio.bridge._visitor import PayloadVisitor, VisitorFunctions
from temporalio.bridge.proto.workflow_activation import workflow_activation_pb2
from temporalio.bridge.proto.workflow_activation.workflow_activation_pb2 import (
    FireTimer,
    ResolveActivity,
    ResolveChildWorkflowExecution,
    ResolveChildWorkflowExecutionStart,
    ResolveNexusOperation,
    ResolveNexusOperationStart,
    ResolveRequestCancelExternalWorkflow,
    ResolveSignalExternalWorkflow,
)
from temporalio.bridge.proto.workflow_commands import workflow_commands_pb2
from temporalio.bridge.proto.workflow_commands.workflow_commands_pb2 import (
    CancelSignalWorkflow,
    CancelTimer,
    RequestCancelActivity,
    RequestCancelExternalWorkflowExecution,
    RequestCancelLocalActivity,
    RequestCancelNexusOperation,
    ScheduleActivity,
    ScheduleLocalActivity,
    ScheduleNexusOperation,
    SignalExternalWorkflowExecution,
    StartChildWorkflowExecution,
    StartTimer,
)


@dataclass(frozen=True)
class CommandInfo:
    """Information identifying a specific command instance."""

    command_type: CommandType.ValueType
    command_seq: int


current_command_info: contextvars.ContextVar[Optional[CommandInfo]] = (
    contextvars.ContextVar("current_command_info", default=None)
)


def _create_override_method(
    parent_method: Any, command_type: CommandType.ValueType
) -> Any:
    """Create an override method that sets command context."""

    async def override_method(self: Any, fs: VisitorFunctions, o: Any) -> None:
        with current_command(command_type, o.seq):
            await parent_method(self, fs, o)

    return override_method


class CommandAwarePayloadVisitor(PayloadVisitor):
    """Payload visitor that sets command context during traversal.

    Override methods are created at class definition time for all workflow
    commands and activation jobs that have a 'seq' field.
    """

    _COMMAND_TYPE_MAP: dict[type[Any], Optional[CommandType.ValueType]] = {
        # Commands
        ScheduleActivity: CommandType.COMMAND_TYPE_SCHEDULE_ACTIVITY_TASK,
        ScheduleLocalActivity: CommandType.COMMAND_TYPE_SCHEDULE_ACTIVITY_TASK,
        StartChildWorkflowExecution: CommandType.COMMAND_TYPE_START_CHILD_WORKFLOW_EXECUTION,
        SignalExternalWorkflowExecution: CommandType.COMMAND_TYPE_SIGNAL_EXTERNAL_WORKFLOW_EXECUTION,
        RequestCancelExternalWorkflowExecution: CommandType.COMMAND_TYPE_REQUEST_CANCEL_EXTERNAL_WORKFLOW_EXECUTION,
        ScheduleNexusOperation: CommandType.COMMAND_TYPE_SCHEDULE_NEXUS_OPERATION,
        RequestCancelNexusOperation: CommandType.COMMAND_TYPE_REQUEST_CANCEL_NEXUS_OPERATION,
        StartTimer: CommandType.COMMAND_TYPE_START_TIMER,
        CancelTimer: CommandType.COMMAND_TYPE_CANCEL_TIMER,
        RequestCancelActivity: CommandType.COMMAND_TYPE_REQUEST_CANCEL_ACTIVITY_TASK,
        RequestCancelLocalActivity: CommandType.COMMAND_TYPE_REQUEST_CANCEL_ACTIVITY_TASK,
        CancelSignalWorkflow: None,
        # Workflow activation jobs
        ResolveActivity: CommandType.COMMAND_TYPE_SCHEDULE_ACTIVITY_TASK,
        ResolveChildWorkflowExecutionStart: CommandType.COMMAND_TYPE_START_CHILD_WORKFLOW_EXECUTION,
        ResolveChildWorkflowExecution: CommandType.COMMAND_TYPE_START_CHILD_WORKFLOW_EXECUTION,
        ResolveSignalExternalWorkflow: CommandType.COMMAND_TYPE_SIGNAL_EXTERNAL_WORKFLOW_EXECUTION,
        ResolveRequestCancelExternalWorkflow: CommandType.COMMAND_TYPE_REQUEST_CANCEL_EXTERNAL_WORKFLOW_EXECUTION,
        ResolveNexusOperationStart: CommandType.COMMAND_TYPE_SCHEDULE_NEXUS_OPERATION,
        ResolveNexusOperation: CommandType.COMMAND_TYPE_SCHEDULE_NEXUS_OPERATION,
        FireTimer: CommandType.COMMAND_TYPE_START_TIMER,
    }


# Add override methods to CommandAwarePayloadVisitor at class definition time
def _add_class_overrides() -> None:
    """Add override methods to CommandAwarePayloadVisitor class."""
    # Process workflow commands
    for proto_class in _get_workflow_command_protos_with_seq():
        if command_type := CommandAwarePayloadVisitor._COMMAND_TYPE_MAP.get(
            proto_class
        ):
            method_name = f"_visit_coresdk_workflow_commands_{proto_class.__name__}"
            parent_method = getattr(PayloadVisitor, method_name, None)
            if parent_method:
                setattr(
                    CommandAwarePayloadVisitor,
                    method_name,
                    _create_override_method(parent_method, command_type),
                )

    # Process activation jobs
    for proto_class in _get_workflow_activation_job_protos_with_seq():
        if command_type := CommandAwarePayloadVisitor._COMMAND_TYPE_MAP.get(
            proto_class
        ):
            method_name = f"_visit_coresdk_workflow_activation_{proto_class.__name__}"
            parent_method = getattr(PayloadVisitor, method_name, None)
            if parent_method:
                setattr(
                    CommandAwarePayloadVisitor,
                    method_name,
                    _create_override_method(parent_method, command_type),
                )


def _get_workflow_command_protos_with_seq() -> Iterator[Type[Any]]:
    """Get concrete classes of all workflow command protos with a seq field."""
    for descriptor in workflow_commands_pb2.DESCRIPTOR.message_types_by_name.values():
        if "seq" in descriptor.fields_by_name:
            yield descriptor._concrete_class


def _get_workflow_activation_job_protos_with_seq() -> Iterator[Type[Any]]:
    """Get concrete classes of all workflow activation job protos with a seq field."""
    for descriptor in workflow_activation_pb2.DESCRIPTOR.message_types_by_name.values():
        if "seq" in descriptor.fields_by_name:
            yield descriptor._concrete_class


@contextmanager
def current_command(
    command_type: CommandType.ValueType, command_seq: int
) -> Iterator[None]:
    """Context manager for setting command info."""
    token = current_command_info.set(
        CommandInfo(command_type=command_type, command_seq=command_seq)
    )
    try:
        yield
    finally:
        if token:
            current_command_info.reset(token)


# Create all override methods on the class when the module is imported
_add_class_overrides()
