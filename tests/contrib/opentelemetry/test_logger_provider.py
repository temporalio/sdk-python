"""Unit tests for ReplaySafeLoggerProvider."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from opentelemetry._logs import (
    Logger,
    LoggerProvider,
    LogRecord,
    NoOpLogger,
    NoOpLoggerProvider,
)
from opentelemetry.sdk._logs import LoggerProvider as SdkLoggerProvider
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.util.types import _ExtendedAttributes

from temporalio import workflow
from temporalio.contrib.opentelemetry import ReplaySafeLoggerProvider


def _sdk_provider() -> tuple[ReplaySafeLoggerProvider, InMemoryLogRecordExporter]:
    exporter = InMemoryLogRecordExporter()
    inner = SdkLoggerProvider()
    inner.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    return ReplaySafeLoggerProvider(inner), exporter


def test_replay_safe_logger_provider_emit_passes_through_outside_workflow():
    provider, exporter = _sdk_provider()
    logger = provider.get_logger("test-logger")

    logger.emit(LogRecord(event_name="record-form", body="hello"))
    logger.emit(event_name="kwargs-form", body="world", attributes={"attr": "val"})

    records = [log.log_record for log in exporter.get_finished_logs()]
    assert [(r.event_name, r.body) for r in records] == [
        ("record-form", "hello"),
        ("kwargs-form", "world"),
    ]
    assert records[1].attributes and dict(records[1].attributes) == {"attr": "val"}


def test_replay_safe_logger_provider_delegates_get_logger_arguments():
    class RecordingLoggerProvider(LoggerProvider):
        def __init__(self) -> None:
            self.calls: list[tuple] = []
            self._inner = SdkLoggerProvider()

        def get_logger(
            self,
            name: str,
            version: str | None = None,
            schema_url: str | None = None,
            attributes: _ExtendedAttributes | None = None,
        ) -> Logger:
            self.calls.append((name, version, schema_url, attributes))
            return self._inner.get_logger(name, version, schema_url, attributes)

    inner_provider = RecordingLoggerProvider()
    provider = ReplaySafeLoggerProvider(inner_provider)
    provider.get_logger(
        "test-logger",
        version="1.2.3",
        schema_url="https://example.com/schema",
        attributes={"attr": "val"},
    )

    assert inner_provider.calls == [
        ("test-logger", "1.2.3", "https://example.com/schema", {"attr": "val"})
    ]


def test_replay_safe_logger_provider_supports_older_otel_signatures():
    """Newer opentelemetry-api parameters (emit keyword fields, 1.38) must
    only be forwarded when the caller passes them, so loggers with older
    signatures keep working."""

    class Pre138Logger(NoOpLogger):
        def __init__(self) -> None:
            super().__init__("pre-1.38-logger")
            self.records: list[LogRecord] = []

        def emit(self, record: LogRecord) -> None:  # type: ignore[override]
            self.records.append(record)

    class Pre138LoggerProvider(NoOpLoggerProvider):
        def __init__(self) -> None:
            self.logger = Pre138Logger()
            self.get_logger_calls: list[tuple[str, str | None, str | None, object]] = []

        def get_logger(  # type: ignore[override]
            self,
            name: str,
            version: str | None = None,
            schema_url: str | None = None,
            attributes: object = None,
        ) -> Logger:
            self.get_logger_calls.append((name, version, schema_url, attributes))
            return self.logger

    inner_provider = Pre138LoggerProvider()
    provider = ReplaySafeLoggerProvider(inner_provider)
    record = LogRecord(event_name="event", body="hello")
    provider.get_logger("test-logger").emit(record)

    assert inner_provider.get_logger_calls == [("test-logger", None, None, None)]
    assert inner_provider.logger.records == [record]


@contextmanager
def _workflow_replay_state(*, replaying_history_events: bool) -> Iterator[None]:
    """Simulate workflow context during replay. When replaying_history_events
    is False this is the query/update-validator state: is_replaying() is True
    but is_replaying_history_events() is False."""
    with (
        patch.object(workflow, "in_workflow", return_value=True),
        patch.object(workflow.unsafe, "is_replaying", return_value=True),
        patch.object(
            workflow.unsafe,
            "is_replaying_history_events",
            return_value=replaying_history_events,
        ),
    ):
        yield


def test_replay_safe_logger_provider_drops_emissions_replaying_history_events():
    provider, exporter = _sdk_provider()
    logger = provider.get_logger("test-logger")

    with _workflow_replay_state(replaying_history_events=True):
        logger.emit(LogRecord(event_name="replayed", body="dropped"))

    assert not exporter.get_finished_logs()


def test_replay_safe_logger_provider_emits_from_live_operations_during_replay():
    """Queries and update validators execute at most once per request even
    when the workflow is replaying, so the gate must use
    is_replaying_history_events(), not is_replaying(), and keep their
    emissions."""
    provider, exporter = _sdk_provider()
    logger = provider.get_logger("test-logger")

    with _workflow_replay_state(replaying_history_events=False):
        logger.emit(LogRecord(event_name="live-query", body="kept"))

    records = [log.log_record for log in exporter.get_finished_logs()]
    assert [(r.event_name, r.body) for r in records] == [("live-query", "kept")]


def test_replay_safe_logger_provider_delegates_other_attributes():
    provider, _ = _sdk_provider()
    assert provider.force_flush()
    provider.shutdown()


def test_replay_safe_logger_provider_delegates_other_logger_attributes():
    class AttributedLogger(NoOpLogger):
        def __init__(self) -> None:
            super().__init__("attributed-logger")
            self.custom = "custom-value"

    class AttributedLoggerProvider(NoOpLoggerProvider):
        def get_logger(self, *args: Any, **kwargs: Any) -> Logger:  # type: ignore[override]
            return AttributedLogger()

    logger = ReplaySafeLoggerProvider(AttributedLoggerProvider()).get_logger("test")
    assert logger.custom == "custom-value"  # type: ignore[attr-defined]
