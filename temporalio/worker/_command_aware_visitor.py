"""Visitor that sets command context during payload traversal."""

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

from temporalio.api.enums.v1.command_type_pb2 import CommandType
from temporalio.bridge._visitor import PayloadVisitor, VisitorFunctions
from temporalio.bridge.proto.workflow_activation.workflow_activation_pb2 import (
    ResolveActivity,
    ResolveChildWorkflowExecution,
    ResolveChildWorkflowExecutionStart,
    ResolveNexusOperation,
    ResolveNexusOperationStart,
    ResolveRequestCancelExternalWorkflow,
    ResolveSignalExternalWorkflow,
)
from temporalio.bridge.proto.workflow_commands.workflow_commands_pb2 import (
    ScheduleActivity,
    ScheduleLocalActivity,
    ScheduleNexusOperation,
    SignalExternalWorkflowExecution,
    StartChildWorkflowExecution,
)


@dataclass(frozen=True)
class CommandInfo:
    """Information identifying a specific command instance."""

    command_type: CommandType.ValueType
    command_seq: int


current_command_info: contextvars.ContextVar[Optional[CommandInfo]] = (
    contextvars.ContextVar("current_command_info", default=None)
)


class CommandAwarePayloadVisitor(PayloadVisitor):
    """Payload visitor that sets command context during traversal.

    Override methods are explicitly defined for workflow commands and
    activation jobs that have both a 'seq' field and payloads to visit.
    """

    # Workflow commands with payloads
    async def _visit_coresdk_workflow_commands_ScheduleActivity(
        self, fs: VisitorFunctions, o: ScheduleActivity
    ) -> None:
        with current_command(CommandType.COMMAND_TYPE_SCHEDULE_ACTIVITY_TASK, o.seq):
            await super()._visit_coresdk_workflow_commands_ScheduleActivity(fs, o)

    async def _visit_coresdk_workflow_commands_ScheduleLocalActivity(
        self, fs: VisitorFunctions, o: ScheduleLocalActivity
    ) -> None:
        with current_command(CommandType.COMMAND_TYPE_SCHEDULE_ACTIVITY_TASK, o.seq):
            await super()._visit_coresdk_workflow_commands_ScheduleLocalActivity(fs, o)

    async def _visit_coresdk_workflow_commands_StartChildWorkflowExecution(
        self, fs: VisitorFunctions, o: StartChildWorkflowExecution
    ) -> None:
        with current_command(
            CommandType.COMMAND_TYPE_START_CHILD_WORKFLOW_EXECUTION, o.seq
        ):
            await super()._visit_coresdk_workflow_commands_StartChildWorkflowExecution(
                fs, o
            )

    async def _visit_coresdk_workflow_commands_SignalExternalWorkflowExecution(
        self, fs: VisitorFunctions, o: SignalExternalWorkflowExecution
    ) -> None:
        with current_command(
            CommandType.COMMAND_TYPE_SIGNAL_EXTERNAL_WORKFLOW_EXECUTION, o.seq
        ):
            await super()._visit_coresdk_workflow_commands_SignalExternalWorkflowExecution(
                fs, o
            )

    async def _visit_coresdk_workflow_commands_ScheduleNexusOperation(
        self, fs: VisitorFunctions, o: ScheduleNexusOperation
    ) -> None:
        with current_command(CommandType.COMMAND_TYPE_SCHEDULE_NEXUS_OPERATION, o.seq):
            await super()._visit_coresdk_workflow_commands_ScheduleNexusOperation(fs, o)

    # Workflow activation jobs with payloads
    async def _visit_coresdk_workflow_activation_ResolveActivity(
        self, fs: VisitorFunctions, o: ResolveActivity
    ) -> None:
        with current_command(CommandType.COMMAND_TYPE_SCHEDULE_ACTIVITY_TASK, o.seq):
            await super()._visit_coresdk_workflow_activation_ResolveActivity(fs, o)

    async def _visit_coresdk_workflow_activation_ResolveChildWorkflowExecutionStart(
        self, fs: VisitorFunctions, o: ResolveChildWorkflowExecutionStart
    ) -> None:
        with current_command(
            CommandType.COMMAND_TYPE_START_CHILD_WORKFLOW_EXECUTION, o.seq
        ):
            await super()._visit_coresdk_workflow_activation_ResolveChildWorkflowExecutionStart(
                fs, o
            )

    async def _visit_coresdk_workflow_activation_ResolveChildWorkflowExecution(
        self, fs: VisitorFunctions, o: ResolveChildWorkflowExecution
    ) -> None:
        with current_command(
            CommandType.COMMAND_TYPE_START_CHILD_WORKFLOW_EXECUTION, o.seq
        ):
            await super()._visit_coresdk_workflow_activation_ResolveChildWorkflowExecution(
                fs, o
            )

    async def _visit_coresdk_workflow_activation_ResolveSignalExternalWorkflow(
        self, fs: VisitorFunctions, o: ResolveSignalExternalWorkflow
    ) -> None:
        with current_command(
            CommandType.COMMAND_TYPE_SIGNAL_EXTERNAL_WORKFLOW_EXECUTION, o.seq
        ):
            await super()._visit_coresdk_workflow_activation_ResolveSignalExternalWorkflow(
                fs, o
            )

    async def _visit_coresdk_workflow_activation_ResolveRequestCancelExternalWorkflow(
        self, fs: VisitorFunctions, o: ResolveRequestCancelExternalWorkflow
    ) -> None:
        with current_command(
            CommandType.COMMAND_TYPE_REQUEST_CANCEL_EXTERNAL_WORKFLOW_EXECUTION, o.seq
        ):
            await super()._visit_coresdk_workflow_activation_ResolveRequestCancelExternalWorkflow(
                fs, o
            )

    async def _visit_coresdk_workflow_activation_ResolveNexusOperationStart(
        self, fs: VisitorFunctions, o: ResolveNexusOperationStart
    ) -> None:
        with current_command(CommandType.COMMAND_TYPE_SCHEDULE_NEXUS_OPERATION, o.seq):
            await super()._visit_coresdk_workflow_activation_ResolveNexusOperationStart(
                fs, o
            )

    async def _visit_coresdk_workflow_activation_ResolveNexusOperation(
        self, fs: VisitorFunctions, o: ResolveNexusOperation
    ) -> None:
        with current_command(CommandType.COMMAND_TYPE_SCHEDULE_NEXUS_OPERATION, o.seq):
            await super()._visit_coresdk_workflow_activation_ResolveNexusOperation(
                fs, o
            )


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
