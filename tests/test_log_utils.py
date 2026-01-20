"""Tests for Temporal logging utilities and LoggerAdapter modes."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from temporalio._log_utils import (
    _apply_temporal_context_to_extra,
    _update_temporal_context_in_extra,
)


class TestApplyTemporalContextToExtra:
    """Tests for _apply_temporal_context_to_extra helper."""

    @pytest.fixture
    def sample_context(self) -> dict[str, Any]:
        return {
            "attempt": 1,
            "namespace": "default",
            "run_id": "abc123",
            "task_queue": "test-queue",
            "workflow_id": "wf-001",
            "workflow_type": "TestWorkflow",
        }

    def test_dict_mode_adds_nested_dict(self, sample_context: dict[str, Any]) -> None:
        extra: dict[str, Any] = {}
        _apply_temporal_context_to_extra(
            extra,
            key="temporal_workflow",
            prefix="temporal.workflow",
            ctx=sample_context,
            mode="dict",
        )

        assert "temporal_workflow" in extra
        assert extra["temporal_workflow"] == sample_context
        # Verify it's a copy, not the same object
        assert extra["temporal_workflow"] is not sample_context

    def test_json_mode_adds_json_string(self, sample_context: dict[str, Any]) -> None:
        extra: dict[str, Any] = {}
        _apply_temporal_context_to_extra(
            extra,
            key="temporal_workflow",
            prefix="temporal.workflow",
            ctx=sample_context,
            mode="json",
        )

        assert "temporal_workflow" in extra
        assert isinstance(extra["temporal_workflow"], str)
        # Verify it's valid JSON
        parsed = json.loads(extra["temporal_workflow"])
        assert parsed == sample_context

    def test_flatten_mode_adds_prefixed_keys(
        self, sample_context: dict[str, Any]
    ) -> None:
        extra: dict[str, Any] = {}
        _apply_temporal_context_to_extra(
            extra,
            key="temporal_workflow",
            prefix="temporal.workflow",
            ctx=sample_context,
            mode="flatten",
        )

        # Should NOT have the nested dict key
        assert "temporal_workflow" not in extra

        # Should have individual prefixed keys
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
            extra,
            key="temporal_test",
            prefix="temporal.test",
            ctx=ctx,
            mode="flatten",
        )

        # Primitives should remain as-is
        assert extra["temporal.test.string_val"] == "hello"
        assert extra["temporal.test.int_val"] == 42
        assert extra["temporal.test.float_val"] == 3.14
        assert extra["temporal.test.bool_val"] is True
        assert extra["temporal.test.none_val"] is None

        # Non-primitives should be converted to strings
        assert extra["temporal.test.list_val"] == "[1, 2, 3]"
        assert extra["temporal.test.dict_val"] == "{'nested': 'value'}"

    def test_flatten_mode_does_not_produce_dict_values(
        self, sample_context: dict[str, Any]
    ) -> None:
        """Verify no value in flattened extra is a dict (OTel compatibility)."""
        extra: dict[str, Any] = {}
        _apply_temporal_context_to_extra(
            extra,
            key="temporal_workflow",
            prefix="temporal.workflow",
            ctx=sample_context,
            mode="flatten",
        )

        for key, value in extra.items():
            if key.startswith("temporal."):
                assert not isinstance(
                    value, dict
                ), f"Key {key} has dict value, not OTel-safe"


class TestUpdateTemporalContextInExtra:
    """Tests for _update_temporal_context_in_extra helper."""

    @pytest.fixture
    def initial_context(self) -> dict[str, Any]:
        return {
            "workflow_id": "wf-001",
            "workflow_type": "TestWorkflow",
        }

    @pytest.fixture
    def update_context(self) -> dict[str, Any]:
        return {
            "update_id": "upd-001",
            "update_name": "my_update",
        }

    def test_dict_mode_updates_existing_dict(
        self, initial_context: dict[str, Any], update_context: dict[str, Any]
    ) -> None:
        extra: dict[str, Any] = {}
        # First apply initial context
        _apply_temporal_context_to_extra(
            extra,
            key="temporal_workflow",
            prefix="temporal.workflow",
            ctx=initial_context,
            mode="dict",
        )
        # Then update
        _update_temporal_context_in_extra(
            extra,
            key="temporal_workflow",
            prefix="temporal.workflow",
            update_ctx=update_context,
            mode="dict",
        )

        assert "temporal_workflow" in extra
        assert extra["temporal_workflow"]["workflow_id"] == "wf-001"
        assert extra["temporal_workflow"]["workflow_type"] == "TestWorkflow"
        assert extra["temporal_workflow"]["update_id"] == "upd-001"
        assert extra["temporal_workflow"]["update_name"] == "my_update"

    def test_json_mode_updates_json_string(
        self, initial_context: dict[str, Any], update_context: dict[str, Any]
    ) -> None:
        extra: dict[str, Any] = {}
        # First apply initial context
        _apply_temporal_context_to_extra(
            extra,
            key="temporal_workflow",
            prefix="temporal.workflow",
            ctx=initial_context,
            mode="json",
        )
        # Then update
        _update_temporal_context_in_extra(
            extra,
            key="temporal_workflow",
            prefix="temporal.workflow",
            update_ctx=update_context,
            mode="json",
        )

        assert "temporal_workflow" in extra
        parsed = json.loads(extra["temporal_workflow"])
        assert parsed["workflow_id"] == "wf-001"
        assert parsed["update_id"] == "upd-001"

    def test_flatten_mode_adds_prefixed_update_keys(
        self, initial_context: dict[str, Any], update_context: dict[str, Any]
    ) -> None:
        extra: dict[str, Any] = {}
        # First apply initial context
        _apply_temporal_context_to_extra(
            extra,
            key="temporal_workflow",
            prefix="temporal.workflow",
            ctx=initial_context,
            mode="flatten",
        )
        # Then update
        _update_temporal_context_in_extra(
            extra,
            key="temporal_workflow",
            prefix="temporal.workflow",
            update_ctx=update_context,
            mode="flatten",
        )

        assert extra["temporal.workflow.workflow_id"] == "wf-001"
        assert extra["temporal.workflow.workflow_type"] == "TestWorkflow"
        assert extra["temporal.workflow.update_id"] == "upd-001"
        assert extra["temporal.workflow.update_name"] == "my_update"


class TestActivityPrefixes:
    """Tests for activity-specific prefixes."""

    @pytest.fixture
    def activity_context(self) -> dict[str, Any]:
        return {
            "activity_id": "act-001",
            "activity_type": "TestActivity",
            "attempt": 1,
            "namespace": "default",
            "task_queue": "test-queue",
            "workflow_id": "wf-001",
            "workflow_run_id": "run-001",
            "workflow_type": "TestWorkflow",
        }

    def test_activity_flatten_uses_activity_prefix(
        self, activity_context: dict[str, Any]
    ) -> None:
        extra: dict[str, Any] = {}
        _apply_temporal_context_to_extra(
            extra,
            key="temporal_activity",
            prefix="temporal.activity",
            ctx=activity_context,
            mode="flatten",
        )

        assert "temporal_activity" not in extra
        assert extra["temporal.activity.activity_id"] == "act-001"
        assert extra["temporal.activity.activity_type"] == "TestActivity"
        assert extra["temporal.activity.attempt"] == 1


class TestNamespaceSafety:
    """Tests to verify flattened keys use proper namespacing."""

    def test_flattened_keys_do_not_conflict_with_logrecord_attrs(self) -> None:
        """Verify temporal prefixed keys won't conflict with LogRecord attributes."""
        # Standard LogRecord attributes that we must not overwrite
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
            "msecs": 999,  # Same name as LogRecord attr
            "name": "workflow-name",  # Same name as LogRecord attr
            "workflow_id": "wf-001",
        }
        extra: dict[str, Any] = {}
        _apply_temporal_context_to_extra(
            extra,
            key="temporal_workflow",
            prefix="temporal.workflow",
            ctx=ctx,
            mode="flatten",
        )

        # None of our flattened keys should be standard LogRecord attrs
        for key in extra.keys():
            assert (
                key not in logrecord_attrs
            ), f"Key {key} conflicts with LogRecord attribute"

        # All our keys should have the temporal prefix
        for key in extra.keys():
            assert key.startswith(
                "temporal."
            ), f"Key {key} doesn't have temporal prefix"


class TestLogRecordAccessibility:
    """Tests to verify flattened attributes are accessible on LogRecord.__dict__."""

    def test_flattened_attrs_accessible_via_record_dict(self) -> None:
        """Verify that flattened attributes can be accessed via LogRecord.__dict__."""
        ctx = {
            "workflow_id": "wf-001",
            "attempt": 1,
        }
        extra: dict[str, Any] = {}
        _apply_temporal_context_to_extra(
            extra,
            key="temporal_workflow",
            prefix="temporal.workflow",
            ctx=ctx,
            mode="flatten",
        )

        # Create a log record with our extra data
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test message",
            args=(),
            exc_info=None,
        )
        # Add our extra to the record
        for key, value in extra.items():
            setattr(record, key, value)

        # Verify accessibility via __dict__
        assert record.__dict__["temporal.workflow.workflow_id"] == "wf-001"
        assert record.__dict__["temporal.workflow.attempt"] == 1

        # Verify all values are primitive (OTel-safe)
        for key in extra.keys():
            value = record.__dict__[key]
            assert isinstance(
                value, (str, int, float, bool, type(None))
            ), f"Value for {key} is not primitive: {type(value)}"
