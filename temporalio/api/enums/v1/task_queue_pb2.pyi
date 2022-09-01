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

class _TaskQueueKind:
    ValueType = typing.NewType("ValueType", builtins.int)
    V: typing_extensions.TypeAlias = ValueType

class _TaskQueueKindEnumTypeWrapper(
    google.protobuf.internal.enum_type_wrapper._EnumTypeWrapper[
        _TaskQueueKind.ValueType
    ],
    builtins.type,
):  # noqa: F821
    DESCRIPTOR: google.protobuf.descriptor.EnumDescriptor
    TASK_QUEUE_KIND_UNSPECIFIED: _TaskQueueKind.ValueType  # 0
    TASK_QUEUE_KIND_NORMAL: _TaskQueueKind.ValueType  # 1
    """Tasks from a normal workflow task queue always include complete workflow history

    The task queue specified by the user is always a normal task queue. There can be as many
    workers as desired for a single normal task queue. All those workers may pick up tasks from
    that queue.
    """
    TASK_QUEUE_KIND_STICKY: _TaskQueueKind.ValueType  # 2
    """A sticky queue only includes new history since the last workflow task, and they are
    per-worker.

    Sticky queues are created dynamically by each worker during their start up. They only exist
    for the lifetime of the worker process. Tasks in a sticky task queue are only available to
    the worker that created the sticky queue.

    Sticky queues are only for workflow tasks. There are no sticky task queues for activities.
    """

class TaskQueueKind(_TaskQueueKind, metaclass=_TaskQueueKindEnumTypeWrapper): ...

TASK_QUEUE_KIND_UNSPECIFIED: TaskQueueKind.ValueType  # 0
TASK_QUEUE_KIND_NORMAL: TaskQueueKind.ValueType  # 1
"""Tasks from a normal workflow task queue always include complete workflow history

The task queue specified by the user is always a normal task queue. There can be as many
workers as desired for a single normal task queue. All those workers may pick up tasks from
that queue.
"""
TASK_QUEUE_KIND_STICKY: TaskQueueKind.ValueType  # 2
"""A sticky queue only includes new history since the last workflow task, and they are
per-worker.

Sticky queues are created dynamically by each worker during their start up. They only exist
for the lifetime of the worker process. Tasks in a sticky task queue are only available to
the worker that created the sticky queue.

Sticky queues are only for workflow tasks. There are no sticky task queues for activities.
"""
global___TaskQueueKind = TaskQueueKind

class _TaskQueueType:
    ValueType = typing.NewType("ValueType", builtins.int)
    V: typing_extensions.TypeAlias = ValueType

class _TaskQueueTypeEnumTypeWrapper(
    google.protobuf.internal.enum_type_wrapper._EnumTypeWrapper[
        _TaskQueueType.ValueType
    ],
    builtins.type,
):  # noqa: F821
    DESCRIPTOR: google.protobuf.descriptor.EnumDescriptor
    TASK_QUEUE_TYPE_UNSPECIFIED: _TaskQueueType.ValueType  # 0
    TASK_QUEUE_TYPE_WORKFLOW: _TaskQueueType.ValueType  # 1
    """Workflow type of task queue."""
    TASK_QUEUE_TYPE_ACTIVITY: _TaskQueueType.ValueType  # 2
    """Activity type of task queue."""

class TaskQueueType(_TaskQueueType, metaclass=_TaskQueueTypeEnumTypeWrapper): ...

TASK_QUEUE_TYPE_UNSPECIFIED: TaskQueueType.ValueType  # 0
TASK_QUEUE_TYPE_WORKFLOW: TaskQueueType.ValueType  # 1
"""Workflow type of task queue."""
TASK_QUEUE_TYPE_ACTIVITY: TaskQueueType.ValueType  # 2
"""Activity type of task queue."""
global___TaskQueueType = TaskQueueType
