"""Tests for Temporal logging utilities and LoggerAdapter modes."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from temporalio._log_utils import (
    _apply_temporal_context_to_extra,
    _update_temporal_context_in_extra,
)


@pytest.fixture
def sample_context() -> dict[str, Any]:
    return {
        "attempt": 1,
        "namespace": "default",
        "run_id": "abc123",
        "task_queue": "test-queue",
        "workflow_id": "wf-001",
        "workflow_type": "TestWorkflow",
    }


class TestApplyTemporalContextToExtra:
    """Tests for _apply_temporal_context_to_extra helper."""

    def test_dict_mode_adds_nested_dict(self, sample_context: dict[str, Any]) -> None:
        extra: dict[str, Any] = {}
        _apply_temporal_context_to_extra(
            extra, key="temporal_workflow", ctx=sample_context, mode="dict"
        )

        assert "temporal_workflow" in extra
        assert extra["temporal_workflow"] == sample_context
        # Verify it's a copy, not the same object
        assert extra["temporal_workflow"] is not sample_context

    def test_flatten_mode_adds_prefixed_keys(
        self, sample_context: dict[str, Any]
    ) -> None:
        extra: dict[str, Any] = {}
        _apply_temporal_context_to_extra(
            extra, key="temporal_workflow", ctx=sample_context, mode="flatten"
        )

        assert "temporal_workflow" not in extra
        assert extra["temporal.workflow.attempt"] == 1
        assert extra["temporal.workflow.namespace"] == "default"
        assert extra["temporal.workflow.run_id"] == "abc123"
        assert extra["temporal.workflow.task_queue"] == "test-queue"
        assert extra["temporal.workflow.workflow_id"] == "wf-001"
        assert extra["temporal.workflow.workflow_type"] == "TestWorkflow"

    def test_flatten_mode_converts_non_primitives_to_string(self) -> None:
        ctx = {
            "string_val": "hello",
            "int_val": 42,
            "float_val": 3.14,
            "bool_val": True,
            "none_val": None,
            "list_val": [1, 2, 3],
            "dict_val": {"nested": "value"},
        }
        extra: dict[str, Any] = {}
        _apply_temporal_context_to_extra(
            extra, key="temporal_test", ctx=ctx, mode="flatten"
        )

        assert extra["temporal.test.string_val"] == "hello"
        assert extra["temporal.test.int_val"] == 42
        assert extra["temporal.test.float_val"] == 3.14
        assert extra["temporal.test.bool_val"] is True
        assert extra["temporal.test.none_val"] is None
        assert extra["temporal.test.list_val"] == "[1, 2, 3]"
        assert extra["temporal.test.dict_val"] == "{'nested': 'value'}"


class TestUpdateTemporalContextInExtra:
    """Tests for _update_temporal_context_in_extra helper."""

    @pytest.fixture
    def initial_context(self) -> dict[str, Any]:
        return {"workflow_id": "wf-001", "workflow_type": "TestWorkflow"}

    @pytest.fixture
    def update_context(self) -> dict[str, Any]:
        return {"update_id": "upd-001", "update_name": "my_update"}

    @pytest.mark.parametrize("mode", ["dict", "flatten"])
    def test_update_merges_context(
        self,
        initial_context: dict[str, Any],
        update_context: dict[str, Any],
        mode: str,
    ) -> None:
        extra: dict[str, Any] = {}
        _apply_temporal_context_to_extra(
            extra, key="temporal_workflow", ctx=initial_context, mode=mode
        )
        _update_temporal_context_in_extra(
            extra, key="temporal_workflow", update_ctx=update_context, mode=mode
        )

        if mode == "dict":
            assert extra["temporal_workflow"]["workflow_id"] == "wf-001"
            assert extra["temporal_workflow"]["workflow_type"] == "TestWorkflow"
            assert extra["temporal_workflow"]["update_id"] == "upd-001"
            assert extra["temporal_workflow"]["update_name"] == "my_update"
        elif mode == "flatten":
            assert extra["temporal.workflow.workflow_id"] == "wf-001"
            assert extra["temporal.workflow.workflow_type"] == "TestWorkflow"
            assert extra["temporal.workflow.update_id"] == "upd-001"
            assert extra["temporal.workflow.update_name"] == "my_update"


class TestActivityPrefixes:
    """Tests for activity-specific key derivation."""

    def test_activity_flatten_uses_activity_prefix(self) -> None:
        ctx = {
            "activity_id": "act-001",
            "activity_type": "TestActivity",
            "attempt": 1,
        }
        extra: dict[str, Any] = {}
        _apply_temporal_context_to_extra(
            extra, key="temporal_activity", ctx=ctx, mode="flatten"
        )

        assert "temporal_activity" not in extra
        assert extra["temporal.activity.activity_id"] == "act-001"
        assert extra["temporal.activity.activity_type"] == "TestActivity"
        assert extra["temporal.activity.attempt"] == 1


class TestFlattenModeOTelSafety:
    """Critical tests to verify flatten mode is fully OTel-safe."""

    @pytest.mark.parametrize("key", ["temporal_workflow", "temporal_activity"])
    def test_flatten_mode_produces_no_dicts(self, key: str) -> None:
        """Verify flatten mode has zero dict values for any temporal keys."""
        ctx = {
            "workflow_id": "wf-001",
            "workflow_type": "TestWorkflow",
            "run_id": "run-001",
            "namespace": "default",
            "task_queue": "test-queue",
            "attempt": 1,
        }
        extra: dict[str, Any] = {}
        _apply_temporal_context_to_extra(extra, key=key, ctx=ctx, mode="flatten")

        for k, v in extra.items():
            assert not isinstance(v, dict), (
                f"Flatten mode violation: {k}={type(v).__name__} "
                f"(expected primitive, got dict)"
            )
        assert key not in extra

    def test_flatten_mode_with_update_produces_no_dicts(self) -> None:
        """Verify flatten mode with update info still produces zero dicts."""
        extra: dict[str, Any] = {}
        _apply_temporal_context_to_extra(
            extra,
            key="temporal_workflow",
            ctx={"workflow_id": "wf-001", "workflow_type": "TestWorkflow"},
            mode="flatten",
        )
        _update_temporal_context_in_extra(
            extra,
            key="temporal_workflow",
            update_ctx={"update_id": "upd-001", "update_name": "my_update"},
            mode="flatten",
        )

        for k, v in extra.items():
            assert not isinstance(
                v, dict
            ), f"Flatten mode violation after update: {k}={type(v).__name__}"
        assert "temporal_workflow" not in extra
        assert extra["temporal.workflow.workflow_id"] == "wf-001"
        assert extra["temporal.workflow.update_id"] == "upd-001"

    def test_flattened_keys_do_not_conflict_with_logrecord_attrs(self) -> None:
        """Verify temporal prefixed keys won't conflict with LogRecord attributes."""
        logrecord_attrs = {
            "name",
            "msg",
            "args",
            "created",
            "filename",
            "funcName",
            "levelname",
            "levelno",
            "lineno",
            "module",
            "msecs",
            "pathname",
            "process",
            "processName",
            "relativeCreated",
            "stack_info",
            "exc_info",
            "exc_text",
            "thread",
            "threadName",
            "message",
        }
        ctx = {
            "msecs": 999,
            "name": "workflow-name",
            "workflow_id": "wf-001",
        }
        extra: dict[str, Any] = {}
        _apply_temporal_context_to_extra(
            extra, key="temporal_workflow", ctx=ctx, mode="flatten"
        )

        for key in extra:
            assert (
                key not in logrecord_attrs
            ), f"Key {key} conflicts with LogRecord attribute"
            assert key.startswith(
                "temporal."
            ), f"Key {key} doesn't have temporal prefix"


class TestLogRecordAccessibility:
    """Tests to verify flattened attributes are accessible on LogRecord.__dict__."""

    def test_flattened_attrs_accessible_via_record_dict(self) -> None:
        ctx = {"workflow_id": "wf-001", "attempt": 1}
        extra: dict[str, Any] = {}
        _apply_temporal_context_to_extra(
            extra, key="temporal_workflow", ctx=ctx, mode="flatten"
        )

        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test message",
            args=(),
            exc_info=None,
        )
        for key, value in extra.items():
            setattr(record, key, value)

        assert record.__dict__["temporal.workflow.workflow_id"] == "wf-001"
        assert record.__dict__["temporal.workflow.attempt"] == 1

        for key in extra:
            value = record.__dict__[key]
            assert isinstance(
                value, (str, int, float, bool, type(None))
            ), f"Value for {key} is not primitive: {type(value)}"
