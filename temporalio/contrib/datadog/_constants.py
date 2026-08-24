"""Package-wide constants for the Datadog tracing interceptor."""

from collections.abc import Mapping
from typing import TypeAlias

import temporalio.api.common.v1

Carrier: TypeAlias = dict[str, str]
StringHeader: TypeAlias = Mapping[str, str]
TemporalHeader: TypeAlias = Mapping[str, temporalio.api.common.v1.Payload]

BAGGAGE_ITEM_SERVICE = "servicename"
CONTINUE_AS_NEW_TAG = "temporal.continued_as_new"
DEFAULT_HEADER_KEY = "dd_trace_span"
TEMPORAL_TAG_PREFIX = "temporal."
_MANUAL_KEEP_TAG = "manual.keep"


class SpanAttributes:
    ACTIVITY_ID = "ActivityID"
    ACTIVITY_TYPE = "ActivityType"
    ATTEMPT = "Attempt"
    CHILD_WORKFLOW_ID = "ChildWorkflowID"
    CHILD_WORKFLOW_TYPE = "ChildWorkflowType"
    EXTERNAL_WORKFLOW_ID = "ExternalWorkflowID"
    LOCAL = "Local"
    NAMESPACE = "Namespace"
    NEXUS_OPERATION = "NexusOperation"
    NEXUS_SERVICE = "NexusService"
    QUERY_TYPE = "QueryType"
    RUN_ID = "RunID"
    SIGNAL_NAME = "SignalName"
    UPDATE_ID = "UpdateID"
    UPDATE_NAME = "UpdateName"
    WORKFLOW_ID = "WorkflowID"
    WORKFLOW_TYPE = "WorkflowType"


COMMON_ATTRIBUTE_MAP: tuple[tuple[str, str], ...] = (
    ("signal", SpanAttributes.SIGNAL_NAME),
    ("query", SpanAttributes.QUERY_TYPE),
    ("activity", SpanAttributes.ACTIVITY_TYPE),
    ("child_workflow_id", SpanAttributes.CHILD_WORKFLOW_ID),
    ("workflow_id", SpanAttributes.EXTERNAL_WORKFLOW_ID),
    ("service", SpanAttributes.NEXUS_SERVICE),
    ("operation_name", SpanAttributes.NEXUS_OPERATION),
)


class OperationNames:
    CREATE_SCHEDULE = "CreateSchedule"
    HANDLE_QUERY = "HandleQuery"
    HANDLE_SIGNAL = "HandleSignal"
    HANDLE_UPDATE = "HandleUpdate"
    QUERY_WORKFLOW = "QueryWorkflow"
    RUN_ACTIVITY = "RunActivity"
    RUN_WORKFLOW = "RunWorkflow"
    SIGNAL_CHILD_WORKFLOW = "SignalChildWorkflow"
    SIGNAL_EXTERNAL_WORKFLOW = "SignalExternalWorkflow"
    SIGNAL_WITH_START_WORKFLOW = "SignalWithStartWorkflow"
    SIGNAL_WORKFLOW = "SignalWorkflow"
    START_ACTIVITY = "StartActivity"
    START_CHILD_WORKFLOW = "StartChildWorkflow"
    START_NEXUS_OPERATION = "StartNexusOperation"
    RUN_NEXUS_OPERATION_START_HANDLER = "RunStartNexusOperationHandler"
    RUN_NEXUS_OPERATION_CANCEL_HANDLER = "RunCancelNexusOperationHandler"
    UPDATE_WITH_START_WORKFLOW = "UpdateWithStartWorkflow"
    UPDATE_WORKFLOW = "UpdateWorkflow"
    START_WORKFLOW = "StartWorkflow"
    VALIDATE_UPDATE = "ValidateUpdate"


# Temporal entry-point operations whose spans are assigned USER_KEEP when they
# have no in-process parent.
# StartActivity is included because clients can start standalone activities
# without a workflow parent.
_MANUAL_KEEP_OPS: frozenset[str] = frozenset(
    {
        OperationNames.RUN_WORKFLOW,
        OperationNames.START_WORKFLOW,
        OperationNames.SIGNAL_WITH_START_WORKFLOW,
        OperationNames.SIGNAL_WORKFLOW,
        OperationNames.QUERY_WORKFLOW,
        OperationNames.UPDATE_WORKFLOW,
        OperationNames.UPDATE_WITH_START_WORKFLOW,
        OperationNames.CREATE_SCHEDULE,
        OperationNames.START_ACTIVITY,
    }
)
