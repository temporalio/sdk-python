from typing import Any, Awaitable, Callable

from temporalio.api.common.v1.message_pb2 import Payload


class PayloadVisitor:
    """A visitor for payloads. Applies a function to every payload in a tree of messages."""

    def __init__(
        self, *, skip_search_attributes: bool = False, skip_headers: bool = False
    ):
        """Creates a new payload visitor."""
        self.skip_search_attributes = skip_search_attributes
        self.skip_headers = skip_headers

    async def visit(
        self, f: Callable[[Payload], Awaitable[Payload]], root: Any
    ) -> None:
        """Visits the given root message with the given function."""
        method_name = "visit_" + root.DESCRIPTOR.full_name.replace(".", "_")
        method = getattr(self, method_name, None)
        if method is not None:
            await method(f, root)

    async def _visit_temporal_api_common_v1_Payload(self, f, o):
        o.CopyFrom(await f(o))

    async def _visit_temporal_api_common_v1_Payloads(self, f, o):
        for v in o.payloads:
            await self._visit_temporal_api_common_v1_Payload(f, v)

    async def _visit_temporal_api_failure_v1_ApplicationFailureInfo(self, f, o):
        if o.HasField("details"):
            await self._visit_temporal_api_common_v1_Payloads(f, o.details)

    async def _visit_temporal_api_failure_v1_TimeoutFailureInfo(self, f, o):
        if o.HasField("last_heartbeat_details"):
            await self._visit_temporal_api_common_v1_Payloads(
                f, o.last_heartbeat_details
            )

    async def _visit_temporal_api_failure_v1_CanceledFailureInfo(self, f, o):
        if o.HasField("details"):
            await self._visit_temporal_api_common_v1_Payloads(f, o.details)

    async def _visit_temporal_api_failure_v1_ResetWorkflowFailureInfo(self, f, o):
        if o.HasField("last_heartbeat_details"):
            await self._visit_temporal_api_common_v1_Payloads(
                f, o.last_heartbeat_details
            )

    async def _visit_temporal_api_failure_v1_Failure(self, f, o):
        if o.HasField("encoded_attributes"):
            await self._visit_temporal_api_common_v1_Payload(f, o.encoded_attributes)
        if o.HasField("application_failure_info"):
            await self._visit_temporal_api_failure_v1_ApplicationFailureInfo(
                f, o.application_failure_info
            )
        if o.HasField("timeout_failure_info"):
            await self._visit_temporal_api_failure_v1_TimeoutFailureInfo(
                f, o.timeout_failure_info
            )
        if o.HasField("canceled_failure_info"):
            await self._visit_temporal_api_failure_v1_CanceledFailureInfo(
                f, o.canceled_failure_info
            )
        if o.HasField("reset_workflow_failure_info"):
            await self._visit_temporal_api_failure_v1_ResetWorkflowFailureInfo(
                f, o.reset_workflow_failure_info
            )

    async def _visit_temporal_api_common_v1_Memo(self, f, o):
        for v in o.fields.values():
            await self._visit_temporal_api_common_v1_Payload(f, v)

    async def _visit_temporal_api_common_v1_SearchAttributes(self, f, o):
        if self.skip_search_attributes:
            return
        for v in o.indexed_fields.values():
            await self._visit_temporal_api_common_v1_Payload(f, v)

    async def _visit_coresdk_workflow_activation_InitializeWorkflow(self, f, o):
        for v in o.arguments:
            await self._visit_temporal_api_common_v1_Payload(f, v)
        if not self.skip_headers:
            for v in o.headers.values():
                await self._visit_temporal_api_common_v1_Payload(f, v)
        if o.HasField("continued_failure"):
            await self._visit_temporal_api_failure_v1_Failure(f, o.continued_failure)
        if o.HasField("last_completion_result"):
            await self._visit_temporal_api_common_v1_Payloads(
                f, o.last_completion_result
            )
        if o.HasField("memo"):
            await self._visit_temporal_api_common_v1_Memo(f, o.memo)
        if o.HasField("search_attributes"):
            await self._visit_temporal_api_common_v1_SearchAttributes(
                f, o.search_attributes
            )

    async def _visit_coresdk_workflow_activation_QueryWorkflow(self, f, o):
        for v in o.arguments:
            await self._visit_temporal_api_common_v1_Payload(f, v)
        if not self.skip_headers:
            for v in o.headers.values():
                await self._visit_temporal_api_common_v1_Payload(f, v)

    async def _visit_coresdk_workflow_activation_SignalWorkflow(self, f, o):
        for v in o.input:
            await self._visit_temporal_api_common_v1_Payload(f, v)
        if not self.skip_headers:
            for v in o.headers.values():
                await self._visit_temporal_api_common_v1_Payload(f, v)

    async def _visit_coresdk_activity_result_Success(self, f, o):
        if o.HasField("result"):
            await self._visit_temporal_api_common_v1_Payload(f, o.result)

    async def _visit_coresdk_activity_result_Failure(self, f, o):
        if o.HasField("failure"):
            await self._visit_temporal_api_failure_v1_Failure(f, o.failure)

    async def _visit_coresdk_activity_result_Cancellation(self, f, o):
        if o.HasField("failure"):
            await self._visit_temporal_api_failure_v1_Failure(f, o.failure)

    async def _visit_coresdk_activity_result_ActivityResolution(self, f, o):
        if o.HasField("completed"):
            await self._visit_coresdk_activity_result_Success(f, o.completed)
        if o.HasField("failed"):
            await self._visit_coresdk_activity_result_Failure(f, o.failed)
        if o.HasField("cancelled"):
            await self._visit_coresdk_activity_result_Cancellation(f, o.cancelled)

    async def _visit_coresdk_workflow_activation_ResolveActivity(self, f, o):
        if o.HasField("result"):
            await self._visit_coresdk_activity_result_ActivityResolution(f, o.result)

    async def _visit_coresdk_workflow_activation_ResolveChildWorkflowExecutionStartCancelled(
        self, f, o
    ):
        if o.HasField("failure"):
            await self._visit_temporal_api_failure_v1_Failure(f, o.failure)

    async def _visit_coresdk_workflow_activation_ResolveChildWorkflowExecutionStart(
        self, f, o
    ):
        if o.HasField("cancelled"):
            await self._visit_coresdk_workflow_activation_ResolveChildWorkflowExecutionStartCancelled(
                f, o.cancelled
            )

    async def _visit_coresdk_child_workflow_Success(self, f, o):
        if o.HasField("result"):
            await self._visit_temporal_api_common_v1_Payload(f, o.result)

    async def _visit_coresdk_child_workflow_Failure(self, f, o):
        if o.HasField("failure"):
            await self._visit_temporal_api_failure_v1_Failure(f, o.failure)

    async def _visit_coresdk_child_workflow_Cancellation(self, f, o):
        if o.HasField("failure"):
            await self._visit_temporal_api_failure_v1_Failure(f, o.failure)

    async def _visit_coresdk_child_workflow_ChildWorkflowResult(self, f, o):
        if o.HasField("completed"):
            await self._visit_coresdk_child_workflow_Success(f, o.completed)
        if o.HasField("failed"):
            await self._visit_coresdk_child_workflow_Failure(f, o.failed)
        if o.HasField("cancelled"):
            await self._visit_coresdk_child_workflow_Cancellation(f, o.cancelled)

    async def _visit_coresdk_workflow_activation_ResolveChildWorkflowExecution(
        self, f, o
    ):
        if o.HasField("result"):
            await self._visit_coresdk_child_workflow_ChildWorkflowResult(f, o.result)

    async def _visit_coresdk_workflow_activation_ResolveSignalExternalWorkflow(
        self, f, o
    ):
        if o.HasField("failure"):
            await self._visit_temporal_api_failure_v1_Failure(f, o.failure)

    async def _visit_coresdk_workflow_activation_ResolveRequestCancelExternalWorkflow(
        self, f, o
    ):
        if o.HasField("failure"):
            await self._visit_temporal_api_failure_v1_Failure(f, o.failure)

    async def _visit_coresdk_workflow_activation_DoUpdate(self, f, o):
        for v in o.input:
            await self._visit_temporal_api_common_v1_Payload(f, v)
        if not self.skip_headers:
            for v in o.headers.values():
                await self._visit_temporal_api_common_v1_Payload(f, v)

    async def _visit_coresdk_workflow_activation_ResolveNexusOperationStart(self, f, o):
        if o.HasField("failed"):
            await self._visit_temporal_api_failure_v1_Failure(f, o.failed)

    async def _visit_coresdk_nexus_NexusOperationResult(self, f, o):
        if o.HasField("completed"):
            await self._visit_temporal_api_common_v1_Payload(f, o.completed)
        if o.HasField("failed"):
            await self._visit_temporal_api_failure_v1_Failure(f, o.failed)
        if o.HasField("cancelled"):
            await self._visit_temporal_api_failure_v1_Failure(f, o.cancelled)
        if o.HasField("timed_out"):
            await self._visit_temporal_api_failure_v1_Failure(f, o.timed_out)

    async def _visit_coresdk_workflow_activation_ResolveNexusOperation(self, f, o):
        if o.HasField("result"):
            await self._visit_coresdk_nexus_NexusOperationResult(f, o.result)

    async def _visit_coresdk_workflow_activation_WorkflowActivationJob(self, f, o):
        if o.HasField("initialize_workflow"):
            await self._visit_coresdk_workflow_activation_InitializeWorkflow(
                f, o.initialize_workflow
            )
        if o.HasField("query_workflow"):
            await self._visit_coresdk_workflow_activation_QueryWorkflow(
                f, o.query_workflow
            )
        if o.HasField("signal_workflow"):
            await self._visit_coresdk_workflow_activation_SignalWorkflow(
                f, o.signal_workflow
            )
        if o.HasField("resolve_activity"):
            await self._visit_coresdk_workflow_activation_ResolveActivity(
                f, o.resolve_activity
            )
        if o.HasField("resolve_child_workflow_execution_start"):
            await self._visit_coresdk_workflow_activation_ResolveChildWorkflowExecutionStart(
                f, o.resolve_child_workflow_execution_start
            )
        if o.HasField("resolve_child_workflow_execution"):
            await self._visit_coresdk_workflow_activation_ResolveChildWorkflowExecution(
                f, o.resolve_child_workflow_execution
            )
        if o.HasField("resolve_signal_external_workflow"):
            await self._visit_coresdk_workflow_activation_ResolveSignalExternalWorkflow(
                f, o.resolve_signal_external_workflow
            )
        if o.HasField("resolve_request_cancel_external_workflow"):
            await self._visit_coresdk_workflow_activation_ResolveRequestCancelExternalWorkflow(
                f, o.resolve_request_cancel_external_workflow
            )
        if o.HasField("do_update"):
            await self._visit_coresdk_workflow_activation_DoUpdate(f, o.do_update)
        if o.HasField("resolve_nexus_operation_start"):
            await self._visit_coresdk_workflow_activation_ResolveNexusOperationStart(
                f, o.resolve_nexus_operation_start
            )
        if o.HasField("resolve_nexus_operation"):
            await self._visit_coresdk_workflow_activation_ResolveNexusOperation(
                f, o.resolve_nexus_operation
            )

    async def _visit_coresdk_workflow_activation_WorkflowActivation(self, f, o):
        for v in o.jobs:
            await self._visit_coresdk_workflow_activation_WorkflowActivationJob(f, v)

    async def _visit_temporal_api_sdk_v1_UserMetadata(self, f, o):
        if o.HasField("summary"):
            await self._visit_temporal_api_common_v1_Payload(f, o.summary)
        if o.HasField("details"):
            await self._visit_temporal_api_common_v1_Payload(f, o.details)

    async def _visit_coresdk_workflow_commands_ScheduleActivity(self, f, o):
        if not self.skip_headers:
            for v in o.headers.values():
                await self._visit_temporal_api_common_v1_Payload(f, v)
        for v in o.arguments:
            await self._visit_temporal_api_common_v1_Payload(f, v)

    async def _visit_coresdk_workflow_commands_QuerySuccess(self, f, o):
        if o.HasField("response"):
            await self._visit_temporal_api_common_v1_Payload(f, o.response)

    async def _visit_coresdk_workflow_commands_QueryResult(self, f, o):
        if o.HasField("succeeded"):
            await self._visit_coresdk_workflow_commands_QuerySuccess(f, o.succeeded)
        if o.HasField("failed"):
            await self._visit_temporal_api_failure_v1_Failure(f, o.failed)

    async def _visit_coresdk_workflow_commands_CompleteWorkflowExecution(self, f, o):
        if o.HasField("result"):
            await self._visit_temporal_api_common_v1_Payload(f, o.result)

    async def _visit_coresdk_workflow_commands_FailWorkflowExecution(self, f, o):
        if o.HasField("failure"):
            await self._visit_temporal_api_failure_v1_Failure(f, o.failure)

    async def _visit_coresdk_workflow_commands_ContinueAsNewWorkflowExecution(
        self, f, o
    ):
        for v in o.arguments:
            await self._visit_temporal_api_common_v1_Payload(f, v)
        for v in o.memo.values():
            await self._visit_temporal_api_common_v1_Payload(f, v)
        if not self.skip_headers:
            for v in o.headers.values():
                await self._visit_temporal_api_common_v1_Payload(f, v)
        for v in o.search_attributes.values():
            await self._visit_temporal_api_common_v1_Payload(f, v)

    async def _visit_coresdk_workflow_commands_StartChildWorkflowExecution(self, f, o):
        for v in o.input:
            await self._visit_temporal_api_common_v1_Payload(f, v)
        if not self.skip_headers:
            for v in o.headers.values():
                await self._visit_temporal_api_common_v1_Payload(f, v)
        for v in o.memo.values():
            await self._visit_temporal_api_common_v1_Payload(f, v)
        for v in o.search_attributes.values():
            await self._visit_temporal_api_common_v1_Payload(f, v)

    async def _visit_coresdk_workflow_commands_SignalExternalWorkflowExecution(
        self, f, o
    ):
        for v in o.args:
            await self._visit_temporal_api_common_v1_Payload(f, v)
        if not self.skip_headers:
            for v in o.headers.values():
                await self._visit_temporal_api_common_v1_Payload(f, v)

    async def _visit_coresdk_workflow_commands_ScheduleLocalActivity(self, f, o):
        if not self.skip_headers:
            for v in o.headers.values():
                await self._visit_temporal_api_common_v1_Payload(f, v)
        for v in o.arguments:
            await self._visit_temporal_api_common_v1_Payload(f, v)

    async def _visit_coresdk_workflow_commands_UpsertWorkflowSearchAttributes(
        self, f, o
    ):
        for v in o.search_attributes.values():
            await self._visit_temporal_api_common_v1_Payload(f, v)

    async def _visit_coresdk_workflow_commands_ModifyWorkflowProperties(self, f, o):
        if o.HasField("upserted_memo"):
            await self._visit_temporal_api_common_v1_Memo(f, o.upserted_memo)

    async def _visit_coresdk_workflow_commands_UpdateResponse(self, f, o):
        if o.HasField("rejected"):
            await self._visit_temporal_api_failure_v1_Failure(f, o.rejected)
        if o.HasField("completed"):
            await self._visit_temporal_api_common_v1_Payload(f, o.completed)

    async def _visit_coresdk_workflow_commands_ScheduleNexusOperation(self, f, o):
        if o.HasField("input"):
            await self._visit_temporal_api_common_v1_Payload(f, o.input)

    async def _visit_coresdk_workflow_commands_WorkflowCommand(self, f, o):
        if o.HasField("user_metadata"):
            await self._visit_temporal_api_sdk_v1_UserMetadata(f, o.user_metadata)
        if o.HasField("schedule_activity"):
            await self._visit_coresdk_workflow_commands_ScheduleActivity(
                f, o.schedule_activity
            )
        if o.HasField("respond_to_query"):
            await self._visit_coresdk_workflow_commands_QueryResult(
                f, o.respond_to_query
            )
        if o.HasField("complete_workflow_execution"):
            await self._visit_coresdk_workflow_commands_CompleteWorkflowExecution(
                f, o.complete_workflow_execution
            )
        if o.HasField("fail_workflow_execution"):
            await self._visit_coresdk_workflow_commands_FailWorkflowExecution(
                f, o.fail_workflow_execution
            )
        if o.HasField("continue_as_new_workflow_execution"):
            await self._visit_coresdk_workflow_commands_ContinueAsNewWorkflowExecution(
                f, o.continue_as_new_workflow_execution
            )
        if o.HasField("start_child_workflow_execution"):
            await self._visit_coresdk_workflow_commands_StartChildWorkflowExecution(
                f, o.start_child_workflow_execution
            )
        if o.HasField("signal_external_workflow_execution"):
            await self._visit_coresdk_workflow_commands_SignalExternalWorkflowExecution(
                f, o.signal_external_workflow_execution
            )
        if o.HasField("schedule_local_activity"):
            await self._visit_coresdk_workflow_commands_ScheduleLocalActivity(
                f, o.schedule_local_activity
            )
        if o.HasField("upsert_workflow_search_attributes"):
            await self._visit_coresdk_workflow_commands_UpsertWorkflowSearchAttributes(
                f, o.upsert_workflow_search_attributes
            )
        if o.HasField("modify_workflow_properties"):
            await self._visit_coresdk_workflow_commands_ModifyWorkflowProperties(
                f, o.modify_workflow_properties
            )
        if o.HasField("update_response"):
            await self._visit_coresdk_workflow_commands_UpdateResponse(
                f, o.update_response
            )
        if o.HasField("schedule_nexus_operation"):
            await self._visit_coresdk_workflow_commands_ScheduleNexusOperation(
                f, o.schedule_nexus_operation
            )

    async def _visit_coresdk_workflow_completion_Success(self, f, o):
        for v in o.commands:
            await self._visit_coresdk_workflow_commands_WorkflowCommand(f, v)

    async def _visit_coresdk_workflow_completion_Failure(self, f, o):
        if o.HasField("failure"):
            await self._visit_temporal_api_failure_v1_Failure(f, o.failure)

    async def _visit_coresdk_workflow_completion_WorkflowActivationCompletion(
        self, f, o
    ):
        if o.HasField("successful"):
            await self._visit_coresdk_workflow_completion_Success(f, o.successful)
        if o.HasField("failed"):
            await self._visit_coresdk_workflow_completion_Failure(f, o.failed)
