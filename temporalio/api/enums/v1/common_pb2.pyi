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
import sys
import typing

import google.protobuf.descriptor
import google.protobuf.internal.enum_type_wrapper

if sys.version_info >= (3, 10):
    import typing as typing_extensions
else:
    import typing_extensions

DESCRIPTOR: google.protobuf.descriptor.FileDescriptor

class _EncodingType:
    ValueType = typing.NewType("ValueType", builtins.int)
    V: typing_extensions.TypeAlias = ValueType

class _EncodingTypeEnumTypeWrapper(
    google.protobuf.internal.enum_type_wrapper._EnumTypeWrapper[
        _EncodingType.ValueType
    ],
    builtins.type,
):  # noqa: F821
    DESCRIPTOR: google.protobuf.descriptor.EnumDescriptor
    ENCODING_TYPE_UNSPECIFIED: _EncodingType.ValueType  # 0
    ENCODING_TYPE_PROTO3: _EncodingType.ValueType  # 1
    ENCODING_TYPE_JSON: _EncodingType.ValueType  # 2

class EncodingType(_EncodingType, metaclass=_EncodingTypeEnumTypeWrapper): ...

ENCODING_TYPE_UNSPECIFIED: EncodingType.ValueType  # 0
ENCODING_TYPE_PROTO3: EncodingType.ValueType  # 1
ENCODING_TYPE_JSON: EncodingType.ValueType  # 2
global___EncodingType = EncodingType

class _IndexedValueType:
    ValueType = typing.NewType("ValueType", builtins.int)
    V: typing_extensions.TypeAlias = ValueType

class _IndexedValueTypeEnumTypeWrapper(
    google.protobuf.internal.enum_type_wrapper._EnumTypeWrapper[
        _IndexedValueType.ValueType
    ],
    builtins.type,
):  # noqa: F821
    DESCRIPTOR: google.protobuf.descriptor.EnumDescriptor
    INDEXED_VALUE_TYPE_UNSPECIFIED: _IndexedValueType.ValueType  # 0
    INDEXED_VALUE_TYPE_TEXT: _IndexedValueType.ValueType  # 1
    INDEXED_VALUE_TYPE_KEYWORD: _IndexedValueType.ValueType  # 2
    INDEXED_VALUE_TYPE_INT: _IndexedValueType.ValueType  # 3
    INDEXED_VALUE_TYPE_DOUBLE: _IndexedValueType.ValueType  # 4
    INDEXED_VALUE_TYPE_BOOL: _IndexedValueType.ValueType  # 5
    INDEXED_VALUE_TYPE_DATETIME: _IndexedValueType.ValueType  # 6
    INDEXED_VALUE_TYPE_KEYWORD_LIST: _IndexedValueType.ValueType  # 7

class IndexedValueType(
    _IndexedValueType, metaclass=_IndexedValueTypeEnumTypeWrapper
): ...

INDEXED_VALUE_TYPE_UNSPECIFIED: IndexedValueType.ValueType  # 0
INDEXED_VALUE_TYPE_TEXT: IndexedValueType.ValueType  # 1
INDEXED_VALUE_TYPE_KEYWORD: IndexedValueType.ValueType  # 2
INDEXED_VALUE_TYPE_INT: IndexedValueType.ValueType  # 3
INDEXED_VALUE_TYPE_DOUBLE: IndexedValueType.ValueType  # 4
INDEXED_VALUE_TYPE_BOOL: IndexedValueType.ValueType  # 5
INDEXED_VALUE_TYPE_DATETIME: IndexedValueType.ValueType  # 6
INDEXED_VALUE_TYPE_KEYWORD_LIST: IndexedValueType.ValueType  # 7
global___IndexedValueType = IndexedValueType

class _Severity:
    ValueType = typing.NewType("ValueType", builtins.int)
    V: typing_extensions.TypeAlias = ValueType

class _SeverityEnumTypeWrapper(
    google.protobuf.internal.enum_type_wrapper._EnumTypeWrapper[_Severity.ValueType],
    builtins.type,
):  # noqa: F821
    DESCRIPTOR: google.protobuf.descriptor.EnumDescriptor
    SEVERITY_UNSPECIFIED: _Severity.ValueType  # 0
    SEVERITY_HIGH: _Severity.ValueType  # 1
    SEVERITY_MEDIUM: _Severity.ValueType  # 2
    SEVERITY_LOW: _Severity.ValueType  # 3

class Severity(_Severity, metaclass=_SeverityEnumTypeWrapper): ...

SEVERITY_UNSPECIFIED: Severity.ValueType  # 0
SEVERITY_HIGH: Severity.ValueType  # 1
SEVERITY_MEDIUM: Severity.ValueType  # 2
SEVERITY_LOW: Severity.ValueType  # 3
global___Severity = Severity

class _CallbackState:
    ValueType = typing.NewType("ValueType", builtins.int)
    V: typing_extensions.TypeAlias = ValueType

class _CallbackStateEnumTypeWrapper(
    google.protobuf.internal.enum_type_wrapper._EnumTypeWrapper[
        _CallbackState.ValueType
    ],
    builtins.type,
):  # noqa: F821
    DESCRIPTOR: google.protobuf.descriptor.EnumDescriptor
    CALLBACK_STATE_UNSPECIFIED: _CallbackState.ValueType  # 0
    """Default value, unspecified state."""
    CALLBACK_STATE_STANDBY: _CallbackState.ValueType  # 1
    """Callback is standing by, waiting to be triggered."""
    CALLBACK_STATE_SCHEDULED: _CallbackState.ValueType  # 2
    """Callback is in the queue waiting to be executed or is currently executing."""
    CALLBACK_STATE_BACKING_OFF: _CallbackState.ValueType  # 3
    """Callback has failed with a retryable error and is backing off before the next attempt."""
    CALLBACK_STATE_FAILED: _CallbackState.ValueType  # 4
    """Callback has failed."""
    CALLBACK_STATE_SUCCEEDED: _CallbackState.ValueType  # 5
    """Callback has succeeded."""
    CALLBACK_STATE_BLOCKED: _CallbackState.ValueType  # 6
    """Callback is blocked (eg: by circuit breaker)."""

class CallbackState(_CallbackState, metaclass=_CallbackStateEnumTypeWrapper):
    """State of a callback."""

CALLBACK_STATE_UNSPECIFIED: CallbackState.ValueType  # 0
"""Default value, unspecified state."""
CALLBACK_STATE_STANDBY: CallbackState.ValueType  # 1
"""Callback is standing by, waiting to be triggered."""
CALLBACK_STATE_SCHEDULED: CallbackState.ValueType  # 2
"""Callback is in the queue waiting to be executed or is currently executing."""
CALLBACK_STATE_BACKING_OFF: CallbackState.ValueType  # 3
"""Callback has failed with a retryable error and is backing off before the next attempt."""
CALLBACK_STATE_FAILED: CallbackState.ValueType  # 4
"""Callback has failed."""
CALLBACK_STATE_SUCCEEDED: CallbackState.ValueType  # 5
"""Callback has succeeded."""
CALLBACK_STATE_BLOCKED: CallbackState.ValueType  # 6
"""Callback is blocked (eg: by circuit breaker)."""
global___CallbackState = CallbackState

class _PendingNexusOperationState:
    ValueType = typing.NewType("ValueType", builtins.int)
    V: typing_extensions.TypeAlias = ValueType

class _PendingNexusOperationStateEnumTypeWrapper(
    google.protobuf.internal.enum_type_wrapper._EnumTypeWrapper[
        _PendingNexusOperationState.ValueType
    ],
    builtins.type,
):  # noqa: F821
    DESCRIPTOR: google.protobuf.descriptor.EnumDescriptor
    PENDING_NEXUS_OPERATION_STATE_UNSPECIFIED: (
        _PendingNexusOperationState.ValueType
    )  # 0
    """Default value, unspecified state."""
    PENDING_NEXUS_OPERATION_STATE_SCHEDULED: _PendingNexusOperationState.ValueType  # 1
    """Operation is in the queue waiting to be executed or is currently executing."""
    PENDING_NEXUS_OPERATION_STATE_BACKING_OFF: (
        _PendingNexusOperationState.ValueType
    )  # 2
    """Operation has failed with a retryable error and is backing off before the next attempt."""
    PENDING_NEXUS_OPERATION_STATE_STARTED: _PendingNexusOperationState.ValueType  # 3
    """Operation was started and will complete asynchronously."""
    PENDING_NEXUS_OPERATION_STATE_BLOCKED: _PendingNexusOperationState.ValueType  # 4
    """Operation is blocked (eg: by circuit breaker)."""

class PendingNexusOperationState(
    _PendingNexusOperationState, metaclass=_PendingNexusOperationStateEnumTypeWrapper
):
    """State of a pending Nexus operation."""

PENDING_NEXUS_OPERATION_STATE_UNSPECIFIED: PendingNexusOperationState.ValueType  # 0
"""Default value, unspecified state."""
PENDING_NEXUS_OPERATION_STATE_SCHEDULED: PendingNexusOperationState.ValueType  # 1
"""Operation is in the queue waiting to be executed or is currently executing."""
PENDING_NEXUS_OPERATION_STATE_BACKING_OFF: PendingNexusOperationState.ValueType  # 2
"""Operation has failed with a retryable error and is backing off before the next attempt."""
PENDING_NEXUS_OPERATION_STATE_STARTED: PendingNexusOperationState.ValueType  # 3
"""Operation was started and will complete asynchronously."""
PENDING_NEXUS_OPERATION_STATE_BLOCKED: PendingNexusOperationState.ValueType  # 4
"""Operation is blocked (eg: by circuit breaker)."""
global___PendingNexusOperationState = PendingNexusOperationState

class _NexusOperationCancellationState:
    ValueType = typing.NewType("ValueType", builtins.int)
    V: typing_extensions.TypeAlias = ValueType

class _NexusOperationCancellationStateEnumTypeWrapper(
    google.protobuf.internal.enum_type_wrapper._EnumTypeWrapper[
        _NexusOperationCancellationState.ValueType
    ],
    builtins.type,
):  # noqa: F821
    DESCRIPTOR: google.protobuf.descriptor.EnumDescriptor
    NEXUS_OPERATION_CANCELLATION_STATE_UNSPECIFIED: (
        _NexusOperationCancellationState.ValueType
    )  # 0
    """Default value, unspecified state."""
    NEXUS_OPERATION_CANCELLATION_STATE_SCHEDULED: (
        _NexusOperationCancellationState.ValueType
    )  # 1
    """Cancellation request is in the queue waiting to be executed or is currently executing."""
    NEXUS_OPERATION_CANCELLATION_STATE_BACKING_OFF: (
        _NexusOperationCancellationState.ValueType
    )  # 2
    """Cancellation request has failed with a retryable error and is backing off before the next attempt."""
    NEXUS_OPERATION_CANCELLATION_STATE_SUCCEEDED: (
        _NexusOperationCancellationState.ValueType
    )  # 3
    """Cancellation request succeeded."""
    NEXUS_OPERATION_CANCELLATION_STATE_FAILED: (
        _NexusOperationCancellationState.ValueType
    )  # 4
    """Cancellation request failed with a non-retryable error."""
    NEXUS_OPERATION_CANCELLATION_STATE_TIMED_OUT: (
        _NexusOperationCancellationState.ValueType
    )  # 5
    """The associated operation timed out - exceeded the user supplied schedule-to-close timeout."""
    NEXUS_OPERATION_CANCELLATION_STATE_BLOCKED: (
        _NexusOperationCancellationState.ValueType
    )  # 6
    """Cancellation request is blocked (eg: by circuit breaker)."""

class NexusOperationCancellationState(
    _NexusOperationCancellationState,
    metaclass=_NexusOperationCancellationStateEnumTypeWrapper,
):
    """State of a Nexus operation cancellation."""

NEXUS_OPERATION_CANCELLATION_STATE_UNSPECIFIED: (
    NexusOperationCancellationState.ValueType
)  # 0
"""Default value, unspecified state."""
NEXUS_OPERATION_CANCELLATION_STATE_SCHEDULED: (
    NexusOperationCancellationState.ValueType
)  # 1
"""Cancellation request is in the queue waiting to be executed or is currently executing."""
NEXUS_OPERATION_CANCELLATION_STATE_BACKING_OFF: (
    NexusOperationCancellationState.ValueType
)  # 2
"""Cancellation request has failed with a retryable error and is backing off before the next attempt."""
NEXUS_OPERATION_CANCELLATION_STATE_SUCCEEDED: (
    NexusOperationCancellationState.ValueType
)  # 3
"""Cancellation request succeeded."""
NEXUS_OPERATION_CANCELLATION_STATE_FAILED: (
    NexusOperationCancellationState.ValueType
)  # 4
"""Cancellation request failed with a non-retryable error."""
NEXUS_OPERATION_CANCELLATION_STATE_TIMED_OUT: (
    NexusOperationCancellationState.ValueType
)  # 5
"""The associated operation timed out - exceeded the user supplied schedule-to-close timeout."""
NEXUS_OPERATION_CANCELLATION_STATE_BLOCKED: (
    NexusOperationCancellationState.ValueType
)  # 6
"""Cancellation request is blocked (eg: by circuit breaker)."""
global___NexusOperationCancellationState = NexusOperationCancellationState
