"""
@generated by mypy-protobuf.  Do not edit manually!
isort:skip_file
The MIT License

Copyright (c) 2020 Temporal Technologies Inc.  All rights reserved.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""
import builtins
import google.protobuf.descriptor
import google.protobuf.internal.enum_type_wrapper
import sys
import typing

if sys.version_info >= (3, 10):
    import typing as typing_extensions
else:
    import typing_extensions

DESCRIPTOR: google.protobuf.descriptor.FileDescriptor

class _WorkflowTaskFailedCause:
    ValueType = typing.NewType("ValueType", builtins.int)
    V: typing_extensions.TypeAlias = ValueType

class _WorkflowTaskFailedCauseEnumTypeWrapper(
    google.protobuf.internal.enum_type_wrapper._EnumTypeWrapper[
        _WorkflowTaskFailedCause.ValueType
    ],
    builtins.type,
):  # noqa: F821
    DESCRIPTOR: google.protobuf.descriptor.EnumDescriptor
    WORKFLOW_TASK_FAILED_CAUSE_UNSPECIFIED: _WorkflowTaskFailedCause.ValueType  # 0
    WORKFLOW_TASK_FAILED_CAUSE_UNHANDLED_COMMAND: _WorkflowTaskFailedCause.ValueType  # 1
    """Between starting and completing the workflow task (with a workflow completion command), some
    new command (like a signal) was processed into workflow history. The outstanding task will be
    failed with this reason, and a worker must pick up a new task.
    """
    WORKFLOW_TASK_FAILED_CAUSE_BAD_SCHEDULE_ACTIVITY_ATTRIBUTES: _WorkflowTaskFailedCause.ValueType  # 2
    WORKFLOW_TASK_FAILED_CAUSE_BAD_REQUEST_CANCEL_ACTIVITY_ATTRIBUTES: _WorkflowTaskFailedCause.ValueType  # 3
    WORKFLOW_TASK_FAILED_CAUSE_BAD_START_TIMER_ATTRIBUTES: _WorkflowTaskFailedCause.ValueType  # 4
    WORKFLOW_TASK_FAILED_CAUSE_BAD_CANCEL_TIMER_ATTRIBUTES: _WorkflowTaskFailedCause.ValueType  # 5
    WORKFLOW_TASK_FAILED_CAUSE_BAD_RECORD_MARKER_ATTRIBUTES: _WorkflowTaskFailedCause.ValueType  # 6
    WORKFLOW_TASK_FAILED_CAUSE_BAD_COMPLETE_WORKFLOW_EXECUTION_ATTRIBUTES: _WorkflowTaskFailedCause.ValueType  # 7
    WORKFLOW_TASK_FAILED_CAUSE_BAD_FAIL_WORKFLOW_EXECUTION_ATTRIBUTES: _WorkflowTaskFailedCause.ValueType  # 8
    WORKFLOW_TASK_FAILED_CAUSE_BAD_CANCEL_WORKFLOW_EXECUTION_ATTRIBUTES: _WorkflowTaskFailedCause.ValueType  # 9
    WORKFLOW_TASK_FAILED_CAUSE_BAD_REQUEST_CANCEL_EXTERNAL_WORKFLOW_EXECUTION_ATTRIBUTES: _WorkflowTaskFailedCause.ValueType  # 10
    WORKFLOW_TASK_FAILED_CAUSE_BAD_CONTINUE_AS_NEW_ATTRIBUTES: _WorkflowTaskFailedCause.ValueType  # 11
    WORKFLOW_TASK_FAILED_CAUSE_START_TIMER_DUPLICATE_ID: _WorkflowTaskFailedCause.ValueType  # 12
    WORKFLOW_TASK_FAILED_CAUSE_RESET_STICKY_TASK_QUEUE: _WorkflowTaskFailedCause.ValueType  # 13
    """The worker wishes to fail the task and have the next one be generated on a normal, not sticky
    queue. Generally workers should prefer to use the explicit `ResetStickyTaskQueue` RPC call.
    """
    WORKFLOW_TASK_FAILED_CAUSE_WORKFLOW_WORKER_UNHANDLED_FAILURE: _WorkflowTaskFailedCause.ValueType  # 14
    WORKFLOW_TASK_FAILED_CAUSE_BAD_SIGNAL_WORKFLOW_EXECUTION_ATTRIBUTES: _WorkflowTaskFailedCause.ValueType  # 15
    WORKFLOW_TASK_FAILED_CAUSE_BAD_START_CHILD_EXECUTION_ATTRIBUTES: _WorkflowTaskFailedCause.ValueType  # 16
    WORKFLOW_TASK_FAILED_CAUSE_FORCE_CLOSE_COMMAND: _WorkflowTaskFailedCause.ValueType  # 17
    WORKFLOW_TASK_FAILED_CAUSE_FAILOVER_CLOSE_COMMAND: _WorkflowTaskFailedCause.ValueType  # 18
    WORKFLOW_TASK_FAILED_CAUSE_BAD_SIGNAL_INPUT_SIZE: _WorkflowTaskFailedCause.ValueType  # 19
    WORKFLOW_TASK_FAILED_CAUSE_RESET_WORKFLOW: _WorkflowTaskFailedCause.ValueType  # 20
    WORKFLOW_TASK_FAILED_CAUSE_BAD_BINARY: _WorkflowTaskFailedCause.ValueType  # 21
    WORKFLOW_TASK_FAILED_CAUSE_SCHEDULE_ACTIVITY_DUPLICATE_ID: _WorkflowTaskFailedCause.ValueType  # 22
    WORKFLOW_TASK_FAILED_CAUSE_BAD_SEARCH_ATTRIBUTES: _WorkflowTaskFailedCause.ValueType  # 23
    WORKFLOW_TASK_FAILED_CAUSE_NON_DETERMINISTIC_ERROR: _WorkflowTaskFailedCause.ValueType  # 24
    """The worker encountered a mismatch while replaying history between what was expected, and
    what the workflow code actually did.
    """
    WORKFLOW_TASK_FAILED_CAUSE_BAD_MODIFY_WORKFLOW_PROPERTIES_ATTRIBUTES: _WorkflowTaskFailedCause.ValueType  # 25
    WORKFLOW_TASK_FAILED_CAUSE_PENDING_CHILD_WORKFLOWS_LIMIT_EXCEEDED: _WorkflowTaskFailedCause.ValueType  # 26
    """We send the below error codes to users when their requests would violate a size constraint
    of their workflow. We do this to ensure that the state of their workflow does not become too
    large because that can cause severe performance degradation. You can modify the thresholds for
    each of these errors within your dynamic config.

    Spawning a new child workflow would cause this workflow to exceed its limit of pending child
    workflows.
    """
    WORKFLOW_TASK_FAILED_CAUSE_PENDING_ACTIVITIES_LIMIT_EXCEEDED: _WorkflowTaskFailedCause.ValueType  # 27
    """Starting a new activity would cause this workflow to exceed its limit of pending activities
    that we track.
    """
    WORKFLOW_TASK_FAILED_CAUSE_PENDING_SIGNALS_LIMIT_EXCEEDED: _WorkflowTaskFailedCause.ValueType  # 28
    """A workflow has a buffer of signals that have not yet reached their destination. We return this
    error when sending a new signal would exceed the capacity of this buffer.
    """
    WORKFLOW_TASK_FAILED_CAUSE_PENDING_REQUEST_CANCEL_LIMIT_EXCEEDED: _WorkflowTaskFailedCause.ValueType  # 29
    """Similarly, we have a buffer of pending requests to cancel other workflows. We return this error
    when our capacity for pending cancel requests is already reached.
    """
    WORKFLOW_TASK_FAILED_CAUSE_BAD_UPDATE_WORKFLOW_EXECUTION_MESSAGE: _WorkflowTaskFailedCause.ValueType  # 30
    """Workflow execution update message (update.Acceptance, update.Rejection, or update.Response)
    has wrong format, or missing required fields.
    """
    WORKFLOW_TASK_FAILED_CAUSE_UNHANDLED_UPDATE: _WorkflowTaskFailedCause.ValueType  # 31
    """Similar to WORKFLOW_TASK_FAILED_CAUSE_UNHANDLED_COMMAND, but for updates."""

class WorkflowTaskFailedCause(
    _WorkflowTaskFailedCause, metaclass=_WorkflowTaskFailedCauseEnumTypeWrapper
):
    """Workflow tasks can fail for various reasons. Note that some of these reasons can only originate
    from the server, and some of them can only originate from the SDK/worker.
    """

WORKFLOW_TASK_FAILED_CAUSE_UNSPECIFIED: WorkflowTaskFailedCause.ValueType  # 0
WORKFLOW_TASK_FAILED_CAUSE_UNHANDLED_COMMAND: WorkflowTaskFailedCause.ValueType  # 1
"""Between starting and completing the workflow task (with a workflow completion command), some
new command (like a signal) was processed into workflow history. The outstanding task will be
failed with this reason, and a worker must pick up a new task.
"""
WORKFLOW_TASK_FAILED_CAUSE_BAD_SCHEDULE_ACTIVITY_ATTRIBUTES: WorkflowTaskFailedCause.ValueType  # 2
WORKFLOW_TASK_FAILED_CAUSE_BAD_REQUEST_CANCEL_ACTIVITY_ATTRIBUTES: WorkflowTaskFailedCause.ValueType  # 3
WORKFLOW_TASK_FAILED_CAUSE_BAD_START_TIMER_ATTRIBUTES: WorkflowTaskFailedCause.ValueType  # 4
WORKFLOW_TASK_FAILED_CAUSE_BAD_CANCEL_TIMER_ATTRIBUTES: WorkflowTaskFailedCause.ValueType  # 5
WORKFLOW_TASK_FAILED_CAUSE_BAD_RECORD_MARKER_ATTRIBUTES: WorkflowTaskFailedCause.ValueType  # 6
WORKFLOW_TASK_FAILED_CAUSE_BAD_COMPLETE_WORKFLOW_EXECUTION_ATTRIBUTES: WorkflowTaskFailedCause.ValueType  # 7
WORKFLOW_TASK_FAILED_CAUSE_BAD_FAIL_WORKFLOW_EXECUTION_ATTRIBUTES: WorkflowTaskFailedCause.ValueType  # 8
WORKFLOW_TASK_FAILED_CAUSE_BAD_CANCEL_WORKFLOW_EXECUTION_ATTRIBUTES: WorkflowTaskFailedCause.ValueType  # 9
WORKFLOW_TASK_FAILED_CAUSE_BAD_REQUEST_CANCEL_EXTERNAL_WORKFLOW_EXECUTION_ATTRIBUTES: WorkflowTaskFailedCause.ValueType  # 10
WORKFLOW_TASK_FAILED_CAUSE_BAD_CONTINUE_AS_NEW_ATTRIBUTES: WorkflowTaskFailedCause.ValueType  # 11
WORKFLOW_TASK_FAILED_CAUSE_START_TIMER_DUPLICATE_ID: WorkflowTaskFailedCause.ValueType  # 12
WORKFLOW_TASK_FAILED_CAUSE_RESET_STICKY_TASK_QUEUE: WorkflowTaskFailedCause.ValueType  # 13
"""The worker wishes to fail the task and have the next one be generated on a normal, not sticky
queue. Generally workers should prefer to use the explicit `ResetStickyTaskQueue` RPC call.
"""
WORKFLOW_TASK_FAILED_CAUSE_WORKFLOW_WORKER_UNHANDLED_FAILURE: WorkflowTaskFailedCause.ValueType  # 14
WORKFLOW_TASK_FAILED_CAUSE_BAD_SIGNAL_WORKFLOW_EXECUTION_ATTRIBUTES: WorkflowTaskFailedCause.ValueType  # 15
WORKFLOW_TASK_FAILED_CAUSE_BAD_START_CHILD_EXECUTION_ATTRIBUTES: WorkflowTaskFailedCause.ValueType  # 16
WORKFLOW_TASK_FAILED_CAUSE_FORCE_CLOSE_COMMAND: WorkflowTaskFailedCause.ValueType  # 17
WORKFLOW_TASK_FAILED_CAUSE_FAILOVER_CLOSE_COMMAND: WorkflowTaskFailedCause.ValueType  # 18
WORKFLOW_TASK_FAILED_CAUSE_BAD_SIGNAL_INPUT_SIZE: WorkflowTaskFailedCause.ValueType  # 19
WORKFLOW_TASK_FAILED_CAUSE_RESET_WORKFLOW: WorkflowTaskFailedCause.ValueType  # 20
WORKFLOW_TASK_FAILED_CAUSE_BAD_BINARY: WorkflowTaskFailedCause.ValueType  # 21
WORKFLOW_TASK_FAILED_CAUSE_SCHEDULE_ACTIVITY_DUPLICATE_ID: WorkflowTaskFailedCause.ValueType  # 22
WORKFLOW_TASK_FAILED_CAUSE_BAD_SEARCH_ATTRIBUTES: WorkflowTaskFailedCause.ValueType  # 23
WORKFLOW_TASK_FAILED_CAUSE_NON_DETERMINISTIC_ERROR: WorkflowTaskFailedCause.ValueType  # 24
"""The worker encountered a mismatch while replaying history between what was expected, and
what the workflow code actually did.
"""
WORKFLOW_TASK_FAILED_CAUSE_BAD_MODIFY_WORKFLOW_PROPERTIES_ATTRIBUTES: WorkflowTaskFailedCause.ValueType  # 25
WORKFLOW_TASK_FAILED_CAUSE_PENDING_CHILD_WORKFLOWS_LIMIT_EXCEEDED: WorkflowTaskFailedCause.ValueType  # 26
"""We send the below error codes to users when their requests would violate a size constraint
of their workflow. We do this to ensure that the state of their workflow does not become too
large because that can cause severe performance degradation. You can modify the thresholds for
each of these errors within your dynamic config.

Spawning a new child workflow would cause this workflow to exceed its limit of pending child
workflows.
"""
WORKFLOW_TASK_FAILED_CAUSE_PENDING_ACTIVITIES_LIMIT_EXCEEDED: WorkflowTaskFailedCause.ValueType  # 27
"""Starting a new activity would cause this workflow to exceed its limit of pending activities
that we track.
"""
WORKFLOW_TASK_FAILED_CAUSE_PENDING_SIGNALS_LIMIT_EXCEEDED: WorkflowTaskFailedCause.ValueType  # 28
"""A workflow has a buffer of signals that have not yet reached their destination. We return this
error when sending a new signal would exceed the capacity of this buffer.
"""
WORKFLOW_TASK_FAILED_CAUSE_PENDING_REQUEST_CANCEL_LIMIT_EXCEEDED: WorkflowTaskFailedCause.ValueType  # 29
"""Similarly, we have a buffer of pending requests to cancel other workflows. We return this error
when our capacity for pending cancel requests is already reached.
"""
WORKFLOW_TASK_FAILED_CAUSE_BAD_UPDATE_WORKFLOW_EXECUTION_MESSAGE: WorkflowTaskFailedCause.ValueType  # 30
"""Workflow execution update message (update.Acceptance, update.Rejection, or update.Response)
has wrong format, or missing required fields.
"""
WORKFLOW_TASK_FAILED_CAUSE_UNHANDLED_UPDATE: WorkflowTaskFailedCause.ValueType  # 31
"""Similar to WORKFLOW_TASK_FAILED_CAUSE_UNHANDLED_COMMAND, but for updates."""
global___WorkflowTaskFailedCause = WorkflowTaskFailedCause

class _StartChildWorkflowExecutionFailedCause:
    ValueType = typing.NewType("ValueType", builtins.int)
    V: typing_extensions.TypeAlias = ValueType

class _StartChildWorkflowExecutionFailedCauseEnumTypeWrapper(
    google.protobuf.internal.enum_type_wrapper._EnumTypeWrapper[
        _StartChildWorkflowExecutionFailedCause.ValueType
    ],
    builtins.type,
):  # noqa: F821
    DESCRIPTOR: google.protobuf.descriptor.EnumDescriptor
    START_CHILD_WORKFLOW_EXECUTION_FAILED_CAUSE_UNSPECIFIED: _StartChildWorkflowExecutionFailedCause.ValueType  # 0
    START_CHILD_WORKFLOW_EXECUTION_FAILED_CAUSE_WORKFLOW_ALREADY_EXISTS: _StartChildWorkflowExecutionFailedCause.ValueType  # 1
    START_CHILD_WORKFLOW_EXECUTION_FAILED_CAUSE_NAMESPACE_NOT_FOUND: _StartChildWorkflowExecutionFailedCause.ValueType  # 2

class StartChildWorkflowExecutionFailedCause(
    _StartChildWorkflowExecutionFailedCause,
    metaclass=_StartChildWorkflowExecutionFailedCauseEnumTypeWrapper,
): ...

START_CHILD_WORKFLOW_EXECUTION_FAILED_CAUSE_UNSPECIFIED: StartChildWorkflowExecutionFailedCause.ValueType  # 0
START_CHILD_WORKFLOW_EXECUTION_FAILED_CAUSE_WORKFLOW_ALREADY_EXISTS: StartChildWorkflowExecutionFailedCause.ValueType  # 1
START_CHILD_WORKFLOW_EXECUTION_FAILED_CAUSE_NAMESPACE_NOT_FOUND: StartChildWorkflowExecutionFailedCause.ValueType  # 2
global___StartChildWorkflowExecutionFailedCause = StartChildWorkflowExecutionFailedCause

class _CancelExternalWorkflowExecutionFailedCause:
    ValueType = typing.NewType("ValueType", builtins.int)
    V: typing_extensions.TypeAlias = ValueType

class _CancelExternalWorkflowExecutionFailedCauseEnumTypeWrapper(
    google.protobuf.internal.enum_type_wrapper._EnumTypeWrapper[
        _CancelExternalWorkflowExecutionFailedCause.ValueType
    ],
    builtins.type,
):  # noqa: F821
    DESCRIPTOR: google.protobuf.descriptor.EnumDescriptor
    CANCEL_EXTERNAL_WORKFLOW_EXECUTION_FAILED_CAUSE_UNSPECIFIED: _CancelExternalWorkflowExecutionFailedCause.ValueType  # 0
    CANCEL_EXTERNAL_WORKFLOW_EXECUTION_FAILED_CAUSE_EXTERNAL_WORKFLOW_EXECUTION_NOT_FOUND: _CancelExternalWorkflowExecutionFailedCause.ValueType  # 1
    CANCEL_EXTERNAL_WORKFLOW_EXECUTION_FAILED_CAUSE_NAMESPACE_NOT_FOUND: _CancelExternalWorkflowExecutionFailedCause.ValueType  # 2

class CancelExternalWorkflowExecutionFailedCause(
    _CancelExternalWorkflowExecutionFailedCause,
    metaclass=_CancelExternalWorkflowExecutionFailedCauseEnumTypeWrapper,
): ...

CANCEL_EXTERNAL_WORKFLOW_EXECUTION_FAILED_CAUSE_UNSPECIFIED: CancelExternalWorkflowExecutionFailedCause.ValueType  # 0
CANCEL_EXTERNAL_WORKFLOW_EXECUTION_FAILED_CAUSE_EXTERNAL_WORKFLOW_EXECUTION_NOT_FOUND: CancelExternalWorkflowExecutionFailedCause.ValueType  # 1
CANCEL_EXTERNAL_WORKFLOW_EXECUTION_FAILED_CAUSE_NAMESPACE_NOT_FOUND: CancelExternalWorkflowExecutionFailedCause.ValueType  # 2
global___CancelExternalWorkflowExecutionFailedCause = (
    CancelExternalWorkflowExecutionFailedCause
)

class _SignalExternalWorkflowExecutionFailedCause:
    ValueType = typing.NewType("ValueType", builtins.int)
    V: typing_extensions.TypeAlias = ValueType

class _SignalExternalWorkflowExecutionFailedCauseEnumTypeWrapper(
    google.protobuf.internal.enum_type_wrapper._EnumTypeWrapper[
        _SignalExternalWorkflowExecutionFailedCause.ValueType
    ],
    builtins.type,
):  # noqa: F821
    DESCRIPTOR: google.protobuf.descriptor.EnumDescriptor
    SIGNAL_EXTERNAL_WORKFLOW_EXECUTION_FAILED_CAUSE_UNSPECIFIED: _SignalExternalWorkflowExecutionFailedCause.ValueType  # 0
    SIGNAL_EXTERNAL_WORKFLOW_EXECUTION_FAILED_CAUSE_EXTERNAL_WORKFLOW_EXECUTION_NOT_FOUND: _SignalExternalWorkflowExecutionFailedCause.ValueType  # 1
    SIGNAL_EXTERNAL_WORKFLOW_EXECUTION_FAILED_CAUSE_NAMESPACE_NOT_FOUND: _SignalExternalWorkflowExecutionFailedCause.ValueType  # 2
    SIGNAL_EXTERNAL_WORKFLOW_EXECUTION_FAILED_CAUSE_SIGNAL_COUNT_LIMIT_EXCEEDED: _SignalExternalWorkflowExecutionFailedCause.ValueType  # 3
    """Signal count limit is per workflow and controlled by server dynamic config "history.maximumSignalsPerExecution" """

class SignalExternalWorkflowExecutionFailedCause(
    _SignalExternalWorkflowExecutionFailedCause,
    metaclass=_SignalExternalWorkflowExecutionFailedCauseEnumTypeWrapper,
): ...

SIGNAL_EXTERNAL_WORKFLOW_EXECUTION_FAILED_CAUSE_UNSPECIFIED: SignalExternalWorkflowExecutionFailedCause.ValueType  # 0
SIGNAL_EXTERNAL_WORKFLOW_EXECUTION_FAILED_CAUSE_EXTERNAL_WORKFLOW_EXECUTION_NOT_FOUND: SignalExternalWorkflowExecutionFailedCause.ValueType  # 1
SIGNAL_EXTERNAL_WORKFLOW_EXECUTION_FAILED_CAUSE_NAMESPACE_NOT_FOUND: SignalExternalWorkflowExecutionFailedCause.ValueType  # 2
SIGNAL_EXTERNAL_WORKFLOW_EXECUTION_FAILED_CAUSE_SIGNAL_COUNT_LIMIT_EXCEEDED: SignalExternalWorkflowExecutionFailedCause.ValueType  # 3
"""Signal count limit is per workflow and controlled by server dynamic config "history.maximumSignalsPerExecution" """
global___SignalExternalWorkflowExecutionFailedCause = (
    SignalExternalWorkflowExecutionFailedCause
)

class _ResourceExhaustedCause:
    ValueType = typing.NewType("ValueType", builtins.int)
    V: typing_extensions.TypeAlias = ValueType

class _ResourceExhaustedCauseEnumTypeWrapper(
    google.protobuf.internal.enum_type_wrapper._EnumTypeWrapper[
        _ResourceExhaustedCause.ValueType
    ],
    builtins.type,
):  # noqa: F821
    DESCRIPTOR: google.protobuf.descriptor.EnumDescriptor
    RESOURCE_EXHAUSTED_CAUSE_UNSPECIFIED: _ResourceExhaustedCause.ValueType  # 0
    RESOURCE_EXHAUSTED_CAUSE_RPS_LIMIT: _ResourceExhaustedCause.ValueType  # 1
    """Caller exceeds request per second limit."""
    RESOURCE_EXHAUSTED_CAUSE_CONCURRENT_LIMIT: _ResourceExhaustedCause.ValueType  # 2
    """Caller exceeds max concurrent request limit."""
    RESOURCE_EXHAUSTED_CAUSE_SYSTEM_OVERLOADED: _ResourceExhaustedCause.ValueType  # 3
    """System overloaded."""
    RESOURCE_EXHAUSTED_CAUSE_PERSISTENCE_LIMIT: _ResourceExhaustedCause.ValueType  # 4
    """Namespace exceeds persistence rate limit."""
    RESOURCE_EXHAUSTED_CAUSE_BUSY_WORKFLOW: _ResourceExhaustedCause.ValueType  # 5
    """Workflow is busy"""

class ResourceExhaustedCause(
    _ResourceExhaustedCause, metaclass=_ResourceExhaustedCauseEnumTypeWrapper
): ...

RESOURCE_EXHAUSTED_CAUSE_UNSPECIFIED: ResourceExhaustedCause.ValueType  # 0
RESOURCE_EXHAUSTED_CAUSE_RPS_LIMIT: ResourceExhaustedCause.ValueType  # 1
"""Caller exceeds request per second limit."""
RESOURCE_EXHAUSTED_CAUSE_CONCURRENT_LIMIT: ResourceExhaustedCause.ValueType  # 2
"""Caller exceeds max concurrent request limit."""
RESOURCE_EXHAUSTED_CAUSE_SYSTEM_OVERLOADED: ResourceExhaustedCause.ValueType  # 3
"""System overloaded."""
RESOURCE_EXHAUSTED_CAUSE_PERSISTENCE_LIMIT: ResourceExhaustedCause.ValueType  # 4
"""Namespace exceeds persistence rate limit."""
RESOURCE_EXHAUSTED_CAUSE_BUSY_WORKFLOW: ResourceExhaustedCause.ValueType  # 5
"""Workflow is busy"""
global___ResourceExhaustedCause = ResourceExhaustedCause
