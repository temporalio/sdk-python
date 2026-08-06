from __future__ import annotations

from typing import TYPE_CHECKING, Any

# opentelemetry._logs is the import path OpenTelemetry itself documents for
# the logs bridge API while it is pre-GA (there is no non-underscore
# counterpart); the only alternative would be not gating ADK-style log
# emission at all.
from opentelemetry._logs import Logger, LoggerProvider

from temporalio import workflow

if TYPE_CHECKING:
    # _ExtendedAttributes is the annotation OpenTelemetry's own public
    # get_logger signature uses; there is no public alias.
    from opentelemetry.util.types import _ExtendedAttributes


def _skip_emitting() -> bool:
    # in_workflow() must be evaluated first: is_replaying() requires an active
    # workflow context.
    return workflow.in_workflow() and workflow.unsafe.is_replaying()


class _ReplaySafeLogger(Logger):
    def __init__(
        self,
        logger: Logger,
        name: str,
        version: str | None = None,
        schema_url: str | None = None,
    ) -> None:
        super().__init__(name, version=version, schema_url=schema_url)
        self._logger = logger

    def __getattr__(self, name: str) -> object:
        return getattr(self._logger, name)

    def emit(self, *args: Any, **kwargs: Any) -> None:
        # emit's signature differs across the supported opentelemetry-api
        # range (a single positional LogRecord through 1.37, keyword fields
        # from 1.38), so forward arguments verbatim rather than pinning one
        # shape; the wrapped logger comes from the same installed API.
        if _skip_emitting():
            # Skip emitting log records during workflow replay to avoid duplicate telemetry
            return
        self._logger.emit(*args, **kwargs)


class ReplaySafeLoggerProvider(LoggerProvider):
    """A logger provider that is safe for use during workflow replay.

    .. warning::
        This class is experimental and may change in future versions.
        Use with caution in production environments.

    This logger provider wraps an OpenTelemetry LoggerProvider and drops log
    records emitted (``Logger.emit()``) from workflow code while the workflow
    is replaying. Without this, libraries that emit OpenTelemetry log records
    from workflow code (e.g. ``google-adk``'s ``gen_ai.*`` events) re-emit
    every record on each replay, duplicating telemetry.

    Emissions are therefore first-execution-only: a workflow task retry
    re-executes live and can emit again. Emissions outside workflows are
    unaffected.

    Install this as the process-global logger provider before any library
    (e.g. ``google-adk``) obtains loggers::

        opentelemetry._logs.set_logger_provider(
            ReplaySafeLoggerProvider(my_logger_provider)
        )

    OpenTelemetry proxy loggers late-bind, so calling ``set_logger_provider``
    after such libraries are imported still routes their loggers through this
    wrapper. However, ``set_logger_provider`` only takes effect once per
    process, so this wrapper must be the one and only global logger provider
    ever set.
    """

    def __init__(self, logger_provider: LoggerProvider) -> None:
        """Initialize the replay-safe logger provider.

        Args:
            logger_provider: The underlying OpenTelemetry LoggerProvider to wrap.
        """
        self._logger_provider = logger_provider

    def __getattr__(self, name: str) -> Any:
        """Delegate all other attributes (e.g. ``shutdown``, ``force_flush``)
        to the underlying logger provider.
        """
        return getattr(self._logger_provider, name)

    def get_logger(
        self,
        name: str,
        version: str | None = None,
        schema_url: str | None = None,
        attributes: _ExtendedAttributes | None = None,
    ) -> Logger:
        """Get a replay-safe logger from the underlying provider.

        Args:
            name: The name of the instrumenting module.
            version: The version string of the instrumenting library.
            schema_url: The schema URL for the logger.
            attributes: Instrumentation scope attributes for the logger.

        Returns:
            A replay-safe logger instance.
        """
        # Forward attributes only when set: the parameter was added in
        # opentelemetry 1.26 and passing it to older providers raises
        # TypeError.
        if attributes is None:
            inner = self._logger_provider.get_logger(name, version, schema_url)
        else:
            inner = self._logger_provider.get_logger(
                name, version, schema_url, attributes
            )
        return _ReplaySafeLogger(inner, name, version=version, schema_url=schema_url)
