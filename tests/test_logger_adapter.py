import logging
from types import MappingProxyType
from unittest.mock import Mock, patch

import pytest

from temporalio import activity, workflow
from temporalio.testing import ActivityEnvironment


@pytest.mark.parametrize(
    "adapter_type",
    [activity.LoggerAdapter, workflow.LoggerAdapter],
    ids=["activity", "workflow"],
)
@pytest.mark.parametrize("include_context", [True, False])
def test_logger_adapter_extra_defaults(
    adapter_type: type[activity.LoggerAdapter] | type[workflow.LoggerAdapter],
    include_context: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter_extra = {"component": "payments", "request_id": "adapter"}
    caller_extra = {"request_id": "caller"}
    adapter = adapter_type(logging.getLogger(__name__), adapter_extra)
    if isinstance(adapter, activity.LoggerAdapter):
        adapter.activity_info_on_message = include_context
        adapter.activity_info_on_extra = include_context
    else:
        adapter.workflow_info_on_message = include_context
        adapter.workflow_info_on_extra = include_context
        adapter.log_during_replay = True

    with caplog.at_level(logging.INFO):
        adapter.info("first", extra=caller_extra)
        adapter.info("second")

    first, second = caplog.records
    assert first.__dict__["component"] == "payments"
    assert first.__dict__["request_id"] == "caller"
    assert second.__dict__["component"] == "payments"
    assert second.__dict__["request_id"] == "adapter"
    assert adapter_extra == {"component": "payments", "request_id": "adapter"}
    assert caller_extra == {"request_id": "caller"}


@pytest.mark.parametrize(
    "adapter_type",
    [activity.LoggerAdapter, workflow.LoggerAdapter],
    ids=["activity", "workflow"],
)
@pytest.mark.parametrize("merge_extra", [True, False])
@pytest.mark.parametrize("adapter_extra", [None, {}, {"component": "payments"}])
@pytest.mark.parametrize(
    "caller_extra", [None, {}, {"component": "checkout", "request_id": "caller"}]
)
def test_logger_adapter_merge_extra(
    adapter_type: type[activity.LoggerAdapter] | type[workflow.LoggerAdapter],
    merge_extra: bool,
    adapter_extra: dict[str, str] | None,
    caller_extra: dict[str, str] | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = adapter_type(
        logging.getLogger(__name__),
        MappingProxyType(adapter_extra) if adapter_extra is not None else None,
        merge_extra=merge_extra,
    )
    if isinstance(adapter, workflow.LoggerAdapter):
        adapter.log_during_replay = True
    with caplog.at_level(logging.INFO):
        adapter.info("hello", extra=caller_extra)

    expected = dict(adapter_extra or {})
    if merge_extra:
        expected.update(caller_extra or {})
    assert {
        key: value
        for key, value in vars(caplog.records[0]).items()
        if key in {"component", "request_id"}
    } == expected


@pytest.mark.parametrize("merge_extra", [True, False])
def test_activity_logger_adapter_extra_with_context(
    merge_extra: bool, caplog: pytest.LogCaptureFixture
) -> None:
    adapter_extra = {"component": "payments"}
    caller_extra = {"request_id": "caller"}
    adapter = activity.LoggerAdapter(
        logging.getLogger(__name__), adapter_extra, merge_extra=merge_extra
    )
    adapter.full_activity_info_on_extra = True
    env = ActivityEnvironment()
    with caplog.at_level(logging.INFO):
        env.run(lambda: adapter.info("hello", extra=caller_extra))

    record = caplog.records[0]
    assert record.__dict__["component"] == "payments"
    assert getattr(record, "request_id", None) == ("caller" if merge_extra else None)
    assert record.__dict__["temporal_activity"] == env.info._logger_details()
    assert record.__dict__["activity_info"] == env.info
    assert adapter_extra == {"component": "payments"}
    assert caller_extra == {"request_id": "caller"}


@pytest.mark.parametrize("merge_extra", [True, False])
def test_workflow_logger_adapter_extra_with_context(
    merge_extra: bool, caplog: pytest.LogCaptureFixture
) -> None:
    adapter_extra = {"component": "payments"}
    caller_extra = {"request_id": "caller"}
    adapter = workflow.LoggerAdapter(
        logging.getLogger(__name__), adapter_extra, merge_extra=merge_extra
    )
    adapter.full_workflow_info_on_extra = True
    runtime = Mock()
    runtime.logger_details = {"workflow_id": "workflow-1"}
    runtime.workflow_is_replaying_history_events.return_value = False
    with (
        patch.object(workflow._Runtime, "maybe_current", return_value=runtime),
        patch.object(workflow._Runtime, "current", return_value=runtime),
        caplog.at_level(logging.INFO),
    ):
        adapter.info("hello", extra=caller_extra)
        runtime.workflow_is_replaying_history_events.return_value = True
        adapter.info("replayed")

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.__dict__["component"] == "payments"
    assert getattr(record, "request_id", None) == ("caller" if merge_extra else None)
    assert record.__dict__["temporal_workflow"] == runtime.logger_details
    assert record.__dict__["workflow_info"] == runtime.workflow_info.return_value
    assert adapter_extra == {"component": "payments"}
    assert caller_extra == {"request_id": "caller"}
