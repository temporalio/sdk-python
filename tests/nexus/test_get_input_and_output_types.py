import warnings
from typing import (
    Any,
    Awaitable,
    Type,
    Union,
    get_args,
    get_origin,
)

import pytest
from nexusrpc.handler import (
    StartOperationContext,
)

from temporalio.nexus.handler._util import (
    _get_start_method_input_and_output_type_annotations,
)


class Input:
    pass


class Output:
    pass


class _TestCase:
    @staticmethod
    def start(ctx: StartOperationContext, i: Input) -> Output: ...

    expected_types: tuple[Any, Any]


class SyncMethod(_TestCase):
    @staticmethod
    def start(ctx: StartOperationContext, i: Input) -> Output: ...

    expected_types = (Input, Output)


class AsyncMethod(_TestCase):
    @staticmethod
    async def start(ctx: StartOperationContext, i: Input) -> Output: ...

    expected_types = (Input, Output)


class UnionMethod(_TestCase):
    @staticmethod
    def start(
        ctx: StartOperationContext, i: Input
    ) -> Union[Output, Awaitable[Output]]: ...

    expected_types = (Input, Union[Output, Awaitable[Output]])


class MissingInputAnnotationInUnionMethod(_TestCase):
    @staticmethod
    def start(ctx: StartOperationContext, i) -> Union[Output, Awaitable[Output]]: ...

    expected_types = (None, Union[Output, Awaitable[Output]])


class TooFewParams(_TestCase):
    @staticmethod
    def start(i: Input) -> Output: ...

    expected_types = (None, Output)


class TooManyParams(_TestCase):
    @staticmethod
    def start(ctx: StartOperationContext, i: Input, extra: int) -> Output: ...

    expected_types = (None, Output)


class WrongOptionsType(_TestCase):
    @staticmethod
    def start(ctx: int, i: Input) -> Output: ...

    expected_types = (None, Output)


class NoReturnHint(_TestCase):
    @staticmethod
    def start(ctx: StartOperationContext, i: Input): ...

    expected_types = (Input, None)


class NoInputAnnotation(_TestCase):
    @staticmethod
    def start(ctx: StartOperationContext, i) -> Output: ...

    expected_types = (None, Output)


class NoOptionsAnnotation(_TestCase):
    @staticmethod
    def start(ctx, i: Input) -> Output: ...

    expected_types = (None, Output)


class AllAnnotationsMissing(_TestCase):
    @staticmethod
    def start(ctx: StartOperationContext, i): ...

    expected_types = (None, None)


class ExplicitNoneTypes(_TestCase):
    @staticmethod
    def start(ctx: StartOperationContext, i: None) -> None: ...

    expected_types = (type(None), type(None))


@pytest.mark.parametrize(
    "test_case",
    [
        SyncMethod,
        AsyncMethod,
        UnionMethod,
        TooFewParams,
        TooManyParams,
        WrongOptionsType,
        NoReturnHint,
        NoInputAnnotation,
        NoOptionsAnnotation,
        MissingInputAnnotationInUnionMethod,
        AllAnnotationsMissing,
        ExplicitNoneTypes,
    ],
)
def test_get_input_and_output_types(test_case: Type[_TestCase]):
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        input_type, output_type = _get_start_method_input_and_output_type_annotations(
            test_case.start
        )
        expected_input_type, expected_output_type = test_case.expected_types
        assert input_type is expected_input_type

        expected_origin = get_origin(expected_output_type)
        if expected_origin:  # Awaitable and Union cases
            assert get_origin(output_type) is expected_origin
            assert get_args(output_type) == get_args(expected_output_type)
        else:
            assert output_type is expected_output_type
