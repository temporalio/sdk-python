"""Visitor that sets command context during payload traversal."""

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from temporalio.api.enums.v1.command_type_pb2 import CommandType
from temporalio.bridge._visitor import PayloadVisitor
from temporalio.bridge._visitor_functions import VisitorFunctions
from temporalio.bridge.proto.workflow_activation.workflow_activation_pb2 import (
    InitializeWorkflow,
    ResolveActivity,
    ResolveChildWorkflowExecution,
    ResolveChildWorkflowExecutionStart,
    ResolveNexusOperation,
    ResolveNexusOperationStart,
    ResolveRequestCancelExternalWorkflow,
    ResolveSignalExternalWorkflow,
)
from temporalio.bridge.proto.workflow_commands.workflow_commands_pb2 import (
    CompleteWorkflowExecution,
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


current_command_info: contextvars.ContextVar[CommandInfo | None] = (
    contextvars.ContextVar("current_command_info", default=None)
)

# Set to the positional index of a workflow run argument while that argument is
# being visited. Lets deferral be decided per argument position (content-neutral)
# rather than by inspecting the payload.
current_run_arg_index: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_run_arg_index", default=None
)


class CommandAwarePayloadVisitor(PayloadVisitor):
    """Payload visitor that sets command context during traversal.

    Override methods are explicitly defined for workflow commands and
    activation jobs that have both a 'seq' field and payloads to visit.
    """

    def __init__(
        self,
        *,
        skip_search_attributes: bool = False,
        skip_headers: bool = False,
        concurrency_limit: int = 1,
        index_run_args: bool = False,
    ) -> None:
        """Creates a new command-aware payload visitor.

        Args:
            skip_search_attributes: If True, search attributes are not visited.
            skip_headers: If True, headers are not visited.
            concurrency_limit: Maximum number of payload visits that may run
                concurrently during a single call to visit(). Defaults to 1.
            index_run_args: If True, workflow run arguments are visited one at a
                time with :py:data:`current_run_arg_index` set, so retrieval can
                be deferred per argument position. Left False (batched) unless a
                run argument is consumed as a PayloadHandle.
        """
        super().__init__(
            skip_search_attributes=skip_search_attributes,
            skip_headers=skip_headers,
            concurrency_limit=concurrency_limit,
        )
        self._index_run_args = index_run_args

    async def _visit_coresdk_workflow_activation_InitializeWorkflow(
        self, fs: VisitorFunctions, o: InitializeWorkflow
    ) -> None:
        if not self._index_run_args:
            await super()._visit_coresdk_workflow_activation_InitializeWorkflow(fs, o)
            return
        # Visit each run argument individually so per-argument-position deferral
        # can be decided, then visit the remaining fields normally. Keep the
        # "remaining fields" list in sync with the generated base method.
        for index, argument in enumerate(o.arguments):
            token = current_run_arg_index.set(index)
            try:
                await self._visit_temporal_api_common_v1_Payload(fs, argument)
            finally:
                current_run_arg_index.reset(token)
        if not self.skip_headers:
            for header in o.headers.values():
                await self._visit_temporal_api_common_v1_Payload(fs, header)
        if o.HasField("continued_failure"):
            await self._visit_temporal_api_failure_v1_Failure(fs, o.continued_failure)
        if o.HasField("last_completion_result"):
            await self._visit_temporal_api_common_v1_Payloads(
                fs, o.last_completion_result
            )
        if o.HasField("memo"):
            await self._visit_temporal_api_common_v1_Memo(fs, o.memo)
        if o.HasField("search_attributes"):
            await self._visit_temporal_api_common_v1_SearchAttributes(
                fs, o.search_attributes
            )

    # Workflow commands with payloads
    async def _visit_coresdk_workflow_commands_CompleteWorkflowExecution(
        self, fs: VisitorFunctions, o: CompleteWorkflowExecution
    ) -> None:
        with current_command(CommandType.COMMAND_TYPE_COMPLETE_WORKFLOW_EXECUTION, 0):
            await super()._visit_coresdk_workflow_commands_CompleteWorkflowExecution(
                fs, o
            )

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
