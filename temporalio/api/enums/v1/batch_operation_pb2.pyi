"""
@generated by mypy-protobuf.  Do not edit manually!
isort:skip_file
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

class _BatchOperationType:
    ValueType = typing.NewType("ValueType", builtins.int)
    V: typing_extensions.TypeAlias = ValueType

class _BatchOperationTypeEnumTypeWrapper(
    google.protobuf.internal.enum_type_wrapper._EnumTypeWrapper[
        _BatchOperationType.ValueType
    ],
    builtins.type,
):  # noqa: F821
    DESCRIPTOR: google.protobuf.descriptor.EnumDescriptor
    BATCH_OPERATION_TYPE_UNSPECIFIED: _BatchOperationType.ValueType  # 0
    BATCH_OPERATION_TYPE_TERMINATE: _BatchOperationType.ValueType  # 1
    BATCH_OPERATION_TYPE_CANCEL: _BatchOperationType.ValueType  # 2
    BATCH_OPERATION_TYPE_SIGNAL: _BatchOperationType.ValueType  # 3
    BATCH_OPERATION_TYPE_DELETE: _BatchOperationType.ValueType  # 4
    BATCH_OPERATION_TYPE_RESET: _BatchOperationType.ValueType  # 5
    BATCH_OPERATION_TYPE_UPDATE_EXECUTION_OPTIONS: _BatchOperationType.ValueType  # 6

class BatchOperationType(
    _BatchOperationType, metaclass=_BatchOperationTypeEnumTypeWrapper
): ...

BATCH_OPERATION_TYPE_UNSPECIFIED: BatchOperationType.ValueType  # 0
BATCH_OPERATION_TYPE_TERMINATE: BatchOperationType.ValueType  # 1
BATCH_OPERATION_TYPE_CANCEL: BatchOperationType.ValueType  # 2
BATCH_OPERATION_TYPE_SIGNAL: BatchOperationType.ValueType  # 3
BATCH_OPERATION_TYPE_DELETE: BatchOperationType.ValueType  # 4
BATCH_OPERATION_TYPE_RESET: BatchOperationType.ValueType  # 5
BATCH_OPERATION_TYPE_UPDATE_EXECUTION_OPTIONS: BatchOperationType.ValueType  # 6
global___BatchOperationType = BatchOperationType

class _BatchOperationState:
    ValueType = typing.NewType("ValueType", builtins.int)
    V: typing_extensions.TypeAlias = ValueType

class _BatchOperationStateEnumTypeWrapper(
    google.protobuf.internal.enum_type_wrapper._EnumTypeWrapper[
        _BatchOperationState.ValueType
    ],
    builtins.type,
):  # noqa: F821
    DESCRIPTOR: google.protobuf.descriptor.EnumDescriptor
    BATCH_OPERATION_STATE_UNSPECIFIED: _BatchOperationState.ValueType  # 0
    BATCH_OPERATION_STATE_RUNNING: _BatchOperationState.ValueType  # 1
    BATCH_OPERATION_STATE_COMPLETED: _BatchOperationState.ValueType  # 2
    BATCH_OPERATION_STATE_FAILED: _BatchOperationState.ValueType  # 3

class BatchOperationState(
    _BatchOperationState, metaclass=_BatchOperationStateEnumTypeWrapper
): ...

BATCH_OPERATION_STATE_UNSPECIFIED: BatchOperationState.ValueType  # 0
BATCH_OPERATION_STATE_RUNNING: BatchOperationState.ValueType  # 1
BATCH_OPERATION_STATE_COMPLETED: BatchOperationState.ValueType  # 2
BATCH_OPERATION_STATE_FAILED: BatchOperationState.ValueType  # 3
global___BatchOperationState = BatchOperationState
