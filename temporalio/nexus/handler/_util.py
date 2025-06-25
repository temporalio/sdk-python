from __future__ import annotations

import functools
import inspect
import typing
import warnings
from typing import (
    Any,
    Awaitable,
    Callable,
    Optional,
    Type,
)

from nexusrpc.handler import (
    StartOperationContext,
    get_start_method_input_and_output_type_annotations,
)
from nexusrpc.types import (
    InputT,
    OutputT,
    ServiceHandlerT,
)

from ._token import (
    WorkflowOperationToken as WorkflowOperationToken,
)


def get_workflow_run_start_method_input_and_output_type_annotations(
    start: Callable[
        [ServiceHandlerT, StartOperationContext, InputT],
        Awaitable[WorkflowOperationToken[OutputT]],
    ],
) -> tuple[
    Optional[Type[InputT]],
    Optional[Type[OutputT]],
]:
    """Return operation input and output types.

    `start` must be a type-annotated start method that returns a
    :py:class:`WorkflowHandle`.
    """
    input_type, output_type = get_start_method_input_and_output_type_annotations(start)
    origin_type = typing.get_origin(output_type)
    if not origin_type:
        output_type = None
    elif not issubclass(origin_type, WorkflowOperationToken):
        warnings.warn(
            f"Expected return type of {start.__name__} to be a subclass of WorkflowOperationToken, "
            f"but is {output_type}"
        )
        output_type = None

    if output_type:
        args = typing.get_args(output_type)
        if len(args) != 1:
            suffix = f": {args}" if args else ""
            warnings.warn(
                f"Expected return type {output_type} of {start.__name__} to have exactly one type parameter, "
                f"but has {len(args)}{suffix}."
            )
            output_type = None
        else:
            [output_type] = args
    return input_type, output_type


# Copied from https://github.com/modelcontextprotocol/python-sdk
#
# Copyright (c) 2024 Anthropic, PBC.
#
# This file is licensed under the MIT License.
def is_async_callable(obj: Any) -> bool:
    """
    Return True if `obj` is an async callable.

    Supports partials of async callable class instances.
    """
    while isinstance(obj, functools.partial):
        obj = obj.func

    return inspect.iscoroutinefunction(obj) or (
        callable(obj) and inspect.iscoroutinefunction(getattr(obj, "__call__", None))
    )
