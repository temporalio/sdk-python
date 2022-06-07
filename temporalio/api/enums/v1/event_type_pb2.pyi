"""
@generated by mypy-protobuf.  Do not edit manually!
isort:skip_file
"""
import builtins
import google.protobuf.descriptor
import google.protobuf.internal.enum_type_wrapper
import typing
import typing_extensions

DESCRIPTOR: google.protobuf.descriptor.FileDescriptor

class _EventType:
    ValueType = typing.NewType("ValueType", builtins.int)
    V: typing_extensions.TypeAlias = ValueType

class _EventTypeEnumTypeWrapper(
    google.protobuf.internal.enum_type_wrapper._EnumTypeWrapper[_EventType.ValueType],
    builtins.type,
):
    DESCRIPTOR: google.protobuf.descriptor.EnumDescriptor
    EVENT_TYPE_UNSPECIFIED: _EventType.ValueType  # 0
    """Place holder and should never appear in a Workflow execution history"""

    EVENT_TYPE_WORKFLOW_EXECUTION_STARTED: _EventType.ValueType  # 1
    """Workflow execution has been triggered/started
    It contains Workflow execution inputs, as well as Workflow timeout configurations
    """

    EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED: _EventType.ValueType  # 2
    """Workflow execution has successfully completed and contains Workflow execution results"""

    EVENT_TYPE_WORKFLOW_EXECUTION_FAILED: _EventType.ValueType  # 3
    """Workflow execution has unsuccessfully completed and contains the Workflow execution error"""

    EVENT_TYPE_WORKFLOW_EXECUTION_TIMED_OUT: _EventType.ValueType  # 4
    """Workflow execution has timed out by the Temporal Server
    Usually due to the Workflow having not been completed within timeout settings
    """

    EVENT_TYPE_WORKFLOW_TASK_SCHEDULED: _EventType.ValueType  # 5
    """Workflow Task has been scheduled and the SDK client should now be able to process any new history events"""

    EVENT_TYPE_WORKFLOW_TASK_STARTED: _EventType.ValueType  # 6
    """Workflow Task has started and the SDK client has picked up the Workflow Task and is processing new history events"""

    EVENT_TYPE_WORKFLOW_TASK_COMPLETED: _EventType.ValueType  # 7
    """Workflow Task has completed
    The SDK client picked up the Workflow Task and processed new history events
    SDK client may or may not ask the Temporal Server to do additional work, such as:
    EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    EVENT_TYPE_TIMER_STARTED
    EVENT_TYPE_UPSERT_WORKFLOW_SEARCH_ATTRIBUTES
    EVENT_TYPE_MARKER_RECORDED
    EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED
    EVENT_TYPE_REQUEST_CANCEL_EXTERNAL_WORKFLOW_EXECUTION_INITIATED
    EVENT_TYPE_SIGNAL_EXTERNAL_WORKFLOW_EXECUTION_INITIATED
    EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED
    EVENT_TYPE_WORKFLOW_EXECUTION_FAILED
    EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED
    EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW
    """

    EVENT_TYPE_WORKFLOW_TASK_TIMED_OUT: _EventType.ValueType  # 8
    """Workflow Task encountered a timeout
    Either an SDK client with a local cache was not available at the time, or it took too long for the SDK client to process the task
    """

    EVENT_TYPE_WORKFLOW_TASK_FAILED: _EventType.ValueType  # 9
    """Workflow Task encountered a failure
    Usually this means that the Workflow was non-deterministic
    However, the Workflow reset functionality also uses this event
    """

    EVENT_TYPE_ACTIVITY_TASK_SCHEDULED: _EventType.ValueType  # 10
    """Activity Task was scheduled
    The SDK client should pick up this activity task and execute
    This event type contains activity inputs, as well as activity timeout configurations
    """

    EVENT_TYPE_ACTIVITY_TASK_STARTED: _EventType.ValueType  # 11
    """Activity Task has started executing
    The SDK client has picked up the Activity Task and is processing the Activity invocation
    """

    EVENT_TYPE_ACTIVITY_TASK_COMPLETED: _EventType.ValueType  # 12
    """Activity Task has finished successfully
    The SDK client has picked up and successfully completed the Activity Task
    This event type contains Activity execution results
    """

    EVENT_TYPE_ACTIVITY_TASK_FAILED: _EventType.ValueType  # 13
    """Activity Task has finished unsuccessfully
    The SDK picked up the Activity Task but unsuccessfully completed it
    This event type contains Activity execution errors
    """

    EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT: _EventType.ValueType  # 14
    """Activity has timed out according to the Temporal Server
    Activity did not complete within the timeout settings
    """

    EVENT_TYPE_ACTIVITY_TASK_CANCEL_REQUESTED: _EventType.ValueType  # 15
    """A request to cancel the Activity has occurred
    The SDK client will be able to confirm cancellation of an Activity during an Activity heartbeat
    """

    EVENT_TYPE_ACTIVITY_TASK_CANCELED: _EventType.ValueType  # 16
    """Activity has been cancelled"""

    EVENT_TYPE_TIMER_STARTED: _EventType.ValueType  # 17
    """A timer has started"""

    EVENT_TYPE_TIMER_FIRED: _EventType.ValueType  # 18
    """A timer has fired"""

    EVENT_TYPE_TIMER_CANCELED: _EventType.ValueType  # 19
    """A time has been cancelled"""

    EVENT_TYPE_WORKFLOW_EXECUTION_CANCEL_REQUESTED: _EventType.ValueType  # 20
    """A request has been made to cancel the Workflow execution"""

    EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED: _EventType.ValueType  # 21
    """SDK client has confirmed the cancellation request and the Workflow execution has been cancelled"""

    EVENT_TYPE_REQUEST_CANCEL_EXTERNAL_WORKFLOW_EXECUTION_INITIATED: _EventType.ValueType  # 22
    """Workflow has requested that the Temporal Server try to cancel another Workflow"""

    EVENT_TYPE_REQUEST_CANCEL_EXTERNAL_WORKFLOW_EXECUTION_FAILED: _EventType.ValueType  # 23
    """Temporal Server could not cancel the targeted Workflow
    This is usually because the target Workflow could not be found
    """

    EVENT_TYPE_EXTERNAL_WORKFLOW_EXECUTION_CANCEL_REQUESTED: _EventType.ValueType  # 24
    """Temporal Server has successfully requested the cancellation of the target Workflow"""

    EVENT_TYPE_MARKER_RECORDED: _EventType.ValueType  # 25
    """A marker has been recorded.
    This event type is transparent to the Temporal Server
    The Server will only store it and will not try to understand it.
    """

    EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED: _EventType.ValueType  # 26
    """Workflow has received a Signal event
    The event type contains the Signal name, as well as a Signal payload
    """

    EVENT_TYPE_WORKFLOW_EXECUTION_TERMINATED: _EventType.ValueType  # 27
    """Workflow execution has been forcefully terminated
    This is usually because the terminate Workflow API was called
    """

    EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW: _EventType.ValueType  # 28
    """Workflow has successfully completed and a new Workflow has been started within the same transaction
    Contains last Workflow execution results as well as new Workflow execution inputs
    """

    EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED: _EventType.ValueType  # 29
    """Temporal Server will try to start a child Workflow"""

    EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_FAILED: _EventType.ValueType  # 30
    """Child Workflow execution cannot be started/triggered
    Usually due to a child Workflow ID collision
    """

    EVENT_TYPE_CHILD_WORKFLOW_EXECUTION_STARTED: _EventType.ValueType  # 31
    """Child Workflow execution has successfully started/triggered"""

    EVENT_TYPE_CHILD_WORKFLOW_EXECUTION_COMPLETED: _EventType.ValueType  # 32
    """Child Workflow execution has successfully completed"""

    EVENT_TYPE_CHILD_WORKFLOW_EXECUTION_FAILED: _EventType.ValueType  # 33
    """Child Workflow execution has unsuccessfully completed"""

    EVENT_TYPE_CHILD_WORKFLOW_EXECUTION_CANCELED: _EventType.ValueType  # 34
    """Child Workflow execution has been cancelled"""

    EVENT_TYPE_CHILD_WORKFLOW_EXECUTION_TIMED_OUT: _EventType.ValueType  # 35
    """Child Workflow execution has timed out by the Temporal Server"""

    EVENT_TYPE_CHILD_WORKFLOW_EXECUTION_TERMINATED: _EventType.ValueType  # 36
    """Child Workflow execution has been terminated"""

    EVENT_TYPE_SIGNAL_EXTERNAL_WORKFLOW_EXECUTION_INITIATED: _EventType.ValueType  # 37
    """Temporal Server will try to Signal the targeted Workflow
    Contains the Signal name, as well as a Signal payload
    """

    EVENT_TYPE_SIGNAL_EXTERNAL_WORKFLOW_EXECUTION_FAILED: _EventType.ValueType  # 38
    """Temporal Server cannot Signal the targeted Workflow
    Usually because the Workflow could not be found
    """

    EVENT_TYPE_EXTERNAL_WORKFLOW_EXECUTION_SIGNALED: _EventType.ValueType  # 39
    """Temporal Server has successfully Signaled the targeted Workflow"""

    EVENT_TYPE_UPSERT_WORKFLOW_SEARCH_ATTRIBUTES: _EventType.ValueType  # 40
    """Workflow search attributes should be updated and synchronized with the visibility store"""

class EventType(_EventType, metaclass=_EventTypeEnumTypeWrapper):
    """Whenever this list of events is changed do change the function shouldBufferEvent in mutableStateBuilder.go to make sure to do the correct event ordering"""

    pass

EVENT_TYPE_UNSPECIFIED: EventType.ValueType  # 0
"""Place holder and should never appear in a Workflow execution history"""

EVENT_TYPE_WORKFLOW_EXECUTION_STARTED: EventType.ValueType  # 1
"""Workflow execution has been triggered/started
It contains Workflow execution inputs, as well as Workflow timeout configurations
"""

EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED: EventType.ValueType  # 2
"""Workflow execution has successfully completed and contains Workflow execution results"""

EVENT_TYPE_WORKFLOW_EXECUTION_FAILED: EventType.ValueType  # 3
"""Workflow execution has unsuccessfully completed and contains the Workflow execution error"""

EVENT_TYPE_WORKFLOW_EXECUTION_TIMED_OUT: EventType.ValueType  # 4
"""Workflow execution has timed out by the Temporal Server
Usually due to the Workflow having not been completed within timeout settings
"""

EVENT_TYPE_WORKFLOW_TASK_SCHEDULED: EventType.ValueType  # 5
"""Workflow Task has been scheduled and the SDK client should now be able to process any new history events"""

EVENT_TYPE_WORKFLOW_TASK_STARTED: EventType.ValueType  # 6
"""Workflow Task has started and the SDK client has picked up the Workflow Task and is processing new history events"""

EVENT_TYPE_WORKFLOW_TASK_COMPLETED: EventType.ValueType  # 7
"""Workflow Task has completed
The SDK client picked up the Workflow Task and processed new history events
SDK client may or may not ask the Temporal Server to do additional work, such as:
EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
EVENT_TYPE_TIMER_STARTED
EVENT_TYPE_UPSERT_WORKFLOW_SEARCH_ATTRIBUTES
EVENT_TYPE_MARKER_RECORDED
EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED
EVENT_TYPE_REQUEST_CANCEL_EXTERNAL_WORKFLOW_EXECUTION_INITIATED
EVENT_TYPE_SIGNAL_EXTERNAL_WORKFLOW_EXECUTION_INITIATED
EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED
EVENT_TYPE_WORKFLOW_EXECUTION_FAILED
EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED
EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW
"""

EVENT_TYPE_WORKFLOW_TASK_TIMED_OUT: EventType.ValueType  # 8
"""Workflow Task encountered a timeout
Either an SDK client with a local cache was not available at the time, or it took too long for the SDK client to process the task
"""

EVENT_TYPE_WORKFLOW_TASK_FAILED: EventType.ValueType  # 9
"""Workflow Task encountered a failure
Usually this means that the Workflow was non-deterministic
However, the Workflow reset functionality also uses this event
"""

EVENT_TYPE_ACTIVITY_TASK_SCHEDULED: EventType.ValueType  # 10
"""Activity Task was scheduled
The SDK client should pick up this activity task and execute
This event type contains activity inputs, as well as activity timeout configurations
"""

EVENT_TYPE_ACTIVITY_TASK_STARTED: EventType.ValueType  # 11
"""Activity Task has started executing
The SDK client has picked up the Activity Task and is processing the Activity invocation
"""

EVENT_TYPE_ACTIVITY_TASK_COMPLETED: EventType.ValueType  # 12
"""Activity Task has finished successfully
The SDK client has picked up and successfully completed the Activity Task
This event type contains Activity execution results
"""

EVENT_TYPE_ACTIVITY_TASK_FAILED: EventType.ValueType  # 13
"""Activity Task has finished unsuccessfully
The SDK picked up the Activity Task but unsuccessfully completed it
This event type contains Activity execution errors
"""

EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT: EventType.ValueType  # 14
"""Activity has timed out according to the Temporal Server
Activity did not complete within the timeout settings
"""

EVENT_TYPE_ACTIVITY_TASK_CANCEL_REQUESTED: EventType.ValueType  # 15
"""A request to cancel the Activity has occurred
The SDK client will be able to confirm cancellation of an Activity during an Activity heartbeat
"""

EVENT_TYPE_ACTIVITY_TASK_CANCELED: EventType.ValueType  # 16
"""Activity has been cancelled"""

EVENT_TYPE_TIMER_STARTED: EventType.ValueType  # 17
"""A timer has started"""

EVENT_TYPE_TIMER_FIRED: EventType.ValueType  # 18
"""A timer has fired"""

EVENT_TYPE_TIMER_CANCELED: EventType.ValueType  # 19
"""A time has been cancelled"""

EVENT_TYPE_WORKFLOW_EXECUTION_CANCEL_REQUESTED: EventType.ValueType  # 20
"""A request has been made to cancel the Workflow execution"""

EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED: EventType.ValueType  # 21
"""SDK client has confirmed the cancellation request and the Workflow execution has been cancelled"""

EVENT_TYPE_REQUEST_CANCEL_EXTERNAL_WORKFLOW_EXECUTION_INITIATED: EventType.ValueType  # 22
"""Workflow has requested that the Temporal Server try to cancel another Workflow"""

EVENT_TYPE_REQUEST_CANCEL_EXTERNAL_WORKFLOW_EXECUTION_FAILED: EventType.ValueType  # 23
"""Temporal Server could not cancel the targeted Workflow
This is usually because the target Workflow could not be found
"""

EVENT_TYPE_EXTERNAL_WORKFLOW_EXECUTION_CANCEL_REQUESTED: EventType.ValueType  # 24
"""Temporal Server has successfully requested the cancellation of the target Workflow"""

EVENT_TYPE_MARKER_RECORDED: EventType.ValueType  # 25
"""A marker has been recorded.
This event type is transparent to the Temporal Server
The Server will only store it and will not try to understand it.
"""

EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED: EventType.ValueType  # 26
"""Workflow has received a Signal event
The event type contains the Signal name, as well as a Signal payload
"""

EVENT_TYPE_WORKFLOW_EXECUTION_TERMINATED: EventType.ValueType  # 27
"""Workflow execution has been forcefully terminated
This is usually because the terminate Workflow API was called
"""

EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW: EventType.ValueType  # 28
"""Workflow has successfully completed and a new Workflow has been started within the same transaction
Contains last Workflow execution results as well as new Workflow execution inputs
"""

EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED: EventType.ValueType  # 29
"""Temporal Server will try to start a child Workflow"""

EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_FAILED: EventType.ValueType  # 30
"""Child Workflow execution cannot be started/triggered
Usually due to a child Workflow ID collision
"""

EVENT_TYPE_CHILD_WORKFLOW_EXECUTION_STARTED: EventType.ValueType  # 31
"""Child Workflow execution has successfully started/triggered"""

EVENT_TYPE_CHILD_WORKFLOW_EXECUTION_COMPLETED: EventType.ValueType  # 32
"""Child Workflow execution has successfully completed"""

EVENT_TYPE_CHILD_WORKFLOW_EXECUTION_FAILED: EventType.ValueType  # 33
"""Child Workflow execution has unsuccessfully completed"""

EVENT_TYPE_CHILD_WORKFLOW_EXECUTION_CANCELED: EventType.ValueType  # 34
"""Child Workflow execution has been cancelled"""

EVENT_TYPE_CHILD_WORKFLOW_EXECUTION_TIMED_OUT: EventType.ValueType  # 35
"""Child Workflow execution has timed out by the Temporal Server"""

EVENT_TYPE_CHILD_WORKFLOW_EXECUTION_TERMINATED: EventType.ValueType  # 36
"""Child Workflow execution has been terminated"""

EVENT_TYPE_SIGNAL_EXTERNAL_WORKFLOW_EXECUTION_INITIATED: EventType.ValueType  # 37
"""Temporal Server will try to Signal the targeted Workflow
Contains the Signal name, as well as a Signal payload
"""

EVENT_TYPE_SIGNAL_EXTERNAL_WORKFLOW_EXECUTION_FAILED: EventType.ValueType  # 38
"""Temporal Server cannot Signal the targeted Workflow
Usually because the Workflow could not be found
"""

EVENT_TYPE_EXTERNAL_WORKFLOW_EXECUTION_SIGNALED: EventType.ValueType  # 39
"""Temporal Server has successfully Signaled the targeted Workflow"""

EVENT_TYPE_UPSERT_WORKFLOW_SEARCH_ATTRIBUTES: EventType.ValueType  # 40
"""Workflow search attributes should be updated and synchronized with the visibility store"""

global___EventType = EventType
