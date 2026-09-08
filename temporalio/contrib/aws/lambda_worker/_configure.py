"""Configuration for the Lambda worker."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta

from temporalio.client import ClientConnectConfig
from temporalio.worker import WorkerConfig

logger = logging.getLogger(__name__)


@dataclass
class LambdaWorkerConfig:
    """Passed to the configure callback of :py:func:`run_worker`.

    Fields are pre-populated with Lambda-appropriate defaults before the configure callback is
    invoked; the callback may read and override any of them.

    Use ``worker_config`` to set task queue, register workflows/activities, and tune worker options.
    The ``task_queue`` key is pre-populated from the ``TEMPORAL_TASK_QUEUE`` environment variable if
    set.

    Attributes:
        client_connect_config: Keyword arguments that will be passed to
            :py:meth:`temporalio.client.Client.connect`. Pre-populated from the
            config file / environment variables via envconfig, with Lambda
            defaults applied.
        worker_config: Keyword arguments that will be passed to the
            :py:class:`temporalio.worker.Worker` constructor (the ``client``
            key is managed internally). Pre-populated with Lambda-appropriate
            defaults (low concurrency, eager activities disabled) and
            ``task_queue`` from ``TEMPORAL_TASK_QUEUE`` if set.
        shutdown_deadline_buffer: How long before the Lambda invocation
            deadline the worker begins its shutdown sequence (worker drain +
            shutdown hooks). Pre-populated to
            ``graceful_shutdown_timeout + 2s``. If you change
            ``graceful_shutdown_timeout`` in ``worker_config``, adjust this
            accordingly.
        shutdown_hooks: Functions called at the end of each Lambda invocation,
            after the worker has stopped. Run in list order. Each may be sync
            or async. Use this to flush telemetry providers or release other
            per-process resources.
    """

    client_connect_config: ClientConnectConfig = field(
        default_factory=ClientConnectConfig
    )
    worker_config: WorkerConfig = field(default_factory=WorkerConfig)
    shutdown_deadline_buffer: timedelta = field(
        default_factory=lambda: timedelta(seconds=7)
    )
    shutdown_hooks: list[Callable[[], Awaitable[None] | None]] = field(
        default_factory=list
    )


async def _run_shutdown_hooks(  # type:ignore[reportUnusedFunction]
    config: LambdaWorkerConfig,
) -> None:
    """Run all registered shutdown hooks in order, logging errors."""
    for fn in config.shutdown_hooks:
        try:
            result = fn()
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            logger.error(f"shutdown hook error: {e}")
