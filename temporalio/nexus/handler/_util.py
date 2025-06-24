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
    Union,
)

from nexusrpc.handler import (
    StartOperationContext,
)
from nexusrpc.types import (
    InputT,
    OutputT,
)

from ._token import (
    WorkflowOperationToken as WorkflowOperationToken,
)


def get_workflow_run_start_method_input_and_output_type_annotations(
    start: Callable[
        [StartOperationContext, InputT],
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
    input_type, output_type = _get_start_method_input_and_output_type_annotations(start)
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


def _get_start_method_input_and_output_type_annotations(
    start: Callable[
        [StartOperationContext, InputT],
        Union[OutputT, Awaitable[OutputT]],
    ],
) -> tuple[
    Optional[Type[InputT]],
    Optional[Type[OutputT]],
]:
    """Return operation input and output types.

    `start` must be a type-annotated start method that returns a synchronous result.
    """
    try:
        type_annotations = typing.get_type_hints(start)
    except TypeError:
        # TODO(nexus-preview): stacklevel
        warnings.warn(
            f"Expected decorated start method {start} to have type annotations"
        )
        return None, None

    if not type_annotations:
        return None, None

    output_type = type_annotations.pop("return", None)

    if len(type_annotations) != 2:
        # TODO(nexus-preview): stacklevel
        suffix = f": {type_annotations}" if type_annotations else ""
        warnings.warn(
            f"Expected decorated start method {start} to have exactly 2 "
            f"type-annotated parameters (ctx and input), but it has {len(type_annotations)}"
            f"{suffix}."
        )
        input_type = None
    else:
        ctx_type, input_type = type_annotations.values()
        if not issubclass(ctx_type, StartOperationContext):
            # TODO(nexus-preview): stacklevel
            warnings.warn(
                f"Expected first parameter of {start} to be an instance of "
                f"StartOperationContext, but is {ctx_type}."
            )
            input_type = None

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
