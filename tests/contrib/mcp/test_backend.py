from typing import Any

import pytest

from temporalio.contrib.mcp._backend import _factory_accepts_argument, _FactoryInvoker
from temporalio.exceptions import ApplicationError


def test_parameterless_factory_is_called_with_no_arguments() -> None:
    invoke = _FactoryInvoker("echo", lambda: "made")
    assert _factory_accepts_argument("echo", lambda: None) is False
    assert invoke() == "made"


def test_single_parameter_factory_receives_none_when_unsupplied() -> None:
    def factory(argument: Any | None) -> Any:
        return argument

    invoke = _FactoryInvoker("echo", factory)
    assert invoke() is None
    assert invoke({"tenant": "acme"}) == {"tenant": "acme"}


def test_defaulted_parameter_factory_receives_the_argument() -> None:
    def factory(argument: Any = "default") -> Any:
        return argument

    invoke = _FactoryInvoker("echo", factory)
    assert invoke() is None
    assert invoke("supplied") == "supplied"


def test_var_positional_factory_receives_the_argument() -> None:
    invoke = _FactoryInvoker("echo", lambda *args: args)
    assert invoke("supplied") == ("supplied",)


def test_var_keyword_only_factory_is_called_with_no_arguments() -> None:
    invoke = _FactoryInvoker("echo", lambda **kwargs: kwargs)
    assert invoke() == {}


def test_argument_for_parameterless_factory_fails_without_retry() -> None:
    invoke = _FactoryInvoker("echo", lambda: "made")
    with pytest.raises(ApplicationError, match="declares no parameters") as err:
        invoke({"tenant": "acme"})
    assert err.value.non_retryable is True


def test_multi_parameter_factory_rejected_at_registration() -> None:
    def factory(first: Any, second: Any) -> Any:
        return first, second

    with pytest.raises(TypeError, match="requires 2 parameters"):
        _FactoryInvoker("echo", factory)


def test_keyword_only_parameter_factory_rejected_at_registration() -> None:
    def factory(*, argument: Any) -> Any:
        return argument

    with pytest.raises(TypeError, match="keyword-only parameter 'argument'"):
        _FactoryInvoker("echo", factory)
