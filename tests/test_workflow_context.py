from datetime import datetime
from typing import Any
from unittest.mock import create_autospec

import pytest

import temporalio.converter
from temporalio._workflow_context import (
    _current_workflow_flags,
    _workflow_flag_scope,
    _WorkflowLogicFlag,
)
from temporalio.worker._workflow_instance import _WorkflowInstanceImpl
from temporalio.worker.workflow_sandbox._importer import Importer
from temporalio.worker.workflow_sandbox._restrictions import (
    RestrictionContext,
    SandboxRestrictions,
)


class FlagRuntime:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.checked: list[_WorkflowLogicFlag] = []
        instance = create_autospec(_WorkflowInstanceImpl, instance=True)
        instance._workflow_logic_flag_enabled.side_effect = (
            self._workflow_logic_flag_enabled
        )
        self.instance: _WorkflowInstanceImpl = instance

    def _workflow_logic_flag_enabled(self, flag: _WorkflowLogicFlag) -> bool:
        self.checked.append(flag)
        return self.enabled


def datetime_hint() -> type:
    with Importer(SandboxRestrictions.default, RestrictionContext()).applied():
        from datetime import datetime as sandboxed_datetime

    return sandboxed_datetime


def test_workflow_flag_scope_restores_context_after_exception() -> None:
    outer, inner = FlagRuntime(True), FlagRuntime(False)
    assert _current_workflow_flags.get() is None
    with _workflow_flag_scope(runtime=outer.instance):
        context = _current_workflow_flags.get()
        assert context is not None
        assert context.is_flag_enabled(_WorkflowLogicFlag.DECODE_DATETIME_PROXIES)
        with (
            pytest.raises(ValueError),
            _workflow_flag_scope(runtime=inner.instance),
        ):
            nested = _current_workflow_flags.get()
            assert nested is not None
            assert not nested.is_flag_enabled(
                _WorkflowLogicFlag.DECODE_DATETIME_PROXIES
            )
            raise ValueError("test cleanup")
        assert _current_workflow_flags.get() is context
    assert _current_workflow_flags.get() is None


def test_datetime_proxy_conversion_policy() -> None:
    hint = datetime_hint()
    assert hint is not datetime
    value = "2021-01-02T00:00:00+00:00"
    with pytest.raises(TypeError):
        temporalio.converter.value_to_type(hint, value)

    disabled = FlagRuntime(False)
    with _workflow_flag_scope(runtime=disabled.instance):
        with pytest.raises(TypeError):
            temporalio.converter.value_to_type(hint, value)
    assert disabled.checked == [_WorkflowLogicFlag.DECODE_DATETIME_PROXIES]

    enabled = FlagRuntime(True)
    with _workflow_flag_scope(runtime=enabled.instance):
        assert temporalio.converter.value_to_type(
            hint, value
        ) == datetime.fromisoformat(value)
    assert enabled.checked == [_WorkflowLogicFlag.DECODE_DATETIME_PROXIES]


def test_datetime_proxy_custom_converter_precedence() -> None:
    expected_hint = datetime_hint()

    class CustomConverter(temporalio.converter.JSONTypeConverter):
        def to_typed_value(self, hint: type, value: Any) -> Any:
            assert hint is expected_hint
            return "custom"

    runtime = FlagRuntime(True)
    with _workflow_flag_scope(runtime=runtime.instance):
        assert (
            temporalio.converter.value_to_type(
                expected_hint, "input", [CustomConverter()]
            )
            == "custom"
        )
    assert not runtime.checked
