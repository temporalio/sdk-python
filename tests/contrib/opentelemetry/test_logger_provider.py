"""Unit tests for ReplaySafeLoggerProvider outside workflows."""

import subprocess
import sys
import textwrap
from typing import Any

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
    """Newer opentelemetry-api parameters (get_logger attributes, 1.26; emit
    keyword fields, 1.38) must only be forwarded when the caller passes them,
    so loggers and providers with older signatures keep working."""

    class Pre138Logger(NoOpLogger):
        def __init__(self) -> None:
            super().__init__("pre-1.38-logger")
            self.records: list[LogRecord] = []

        def emit(self, record: LogRecord) -> None:  # type: ignore[override]
            self.records.append(record)

    class Pre126LoggerProvider(NoOpLoggerProvider):
        def __init__(self) -> None:
            self.logger = Pre138Logger()
            self.get_logger_calls: list[tuple[str, str | None, str | None]] = []

        def get_logger(  # type: ignore[override]
            self,
            name: str,
            version: str | None = None,
            schema_url: str | None = None,
        ) -> Logger:
            self.get_logger_calls.append((name, version, schema_url))
            return self.logger

    inner_provider = Pre126LoggerProvider()
    provider = ReplaySafeLoggerProvider(inner_provider)
    record = LogRecord(event_name="event", body="hello")
    provider.get_logger("test-logger").emit(record)

    assert inner_provider.get_logger_calls == [("test-logger", None, None)]
    assert inner_provider.logger.records == [record]


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


def _run_in_subprocess(code: str) -> None:
    # Import-time behavior must be tested in a fresh interpreter so the
    # simulated old opentelemetry-api is seen before temporalio imports it and
    # no module state leaks into other tests.
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"


def test_tracing_importable_without_logs_api():
    """opentelemetry-api < 1.15 has no opentelemetry._logs module at all;
    tracing and metrics users must be unaffected and ReplaySafeLoggerProvider
    must raise an actionable error on access. Blocking opentelemetry._logs
    itself would also break the modern opentelemetry-sdk installed here, so
    simulate by failing the guarded submodule import."""
    _run_in_subprocess(
        """
        import sys

        sys.modules["temporalio.contrib.opentelemetry._logger_provider"] = None

        import temporalio.contrib.opentelemetry as otel_contrib

        assert otel_contrib.ReplaySafeTracerProvider is not None
        assert otel_contrib.ReplaySafeMeterProvider is not None
        assert otel_contrib.create_tracer_provider is not None
        try:
            otel_contrib.ReplaySafeLoggerProvider
        except ImportError as err:
            assert "opentelemetry-api >= 1.15" in str(err), str(err)
        else:
            raise AssertionError("expected ImportError accessing ReplaySafeLoggerProvider")
        """
    )
