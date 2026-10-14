"""Configuration for a Temporal worker running in a Modal Function."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta

from temporalio.client import ClientConnectConfig
from temporalio.contrib.modal.worker._defaults import DEFAULT_EXECUTION_TIMEOUT
from temporalio.worker import WorkerConfig

logger = logging.getLogger(__name__)


@dataclass
class ModalWorkerConfig:
    """Configuration passed to :py:func:`run_worker`'s callback.

    Attributes:
        client_connect_config: Keyword arguments passed to
            :py:meth:`temporalio.client.Client.connect`. Values are loaded from
            Temporal environment configuration before the callback runs.
        worker_config: Keyword arguments passed to
            :py:class:`temporalio.worker.Worker`. The ``client`` key is managed
            internally. Conservative concurrency and polling defaults are
            applied, and ``task_queue`` is populated from
            ``TEMPORAL_TASK_QUEUE`` when present.
        execution_timeout: Total time budget for this invocation, including
            configuration and connection setup. It defaults to five minutes
            and must not exceed the ``timeout`` configured on
            ``@app.function``. Leave additional Modal timeout headroom for code
            that runs before or after :py:func:`run_worker`.
        shutdown_deadline_buffer: Time reserved at the end of the invocation
            for worker drain and shutdown hooks. If ``graceful_shutdown_timeout``
            changes, adjust this value accordingly.
        shutdown_hooks: Sync or async functions called in order after the
            worker stops, including when worker execution fails or is cancelled.
    """

    client_connect_config: ClientConnectConfig = field(
        default_factory=ClientConnectConfig
    )
    worker_config: WorkerConfig = field(default_factory=WorkerConfig)
    execution_timeout: timedelta = DEFAULT_EXECUTION_TIMEOUT
    shutdown_deadline_buffer: timedelta = field(
        default_factory=lambda: timedelta(seconds=7)
    )
    shutdown_hooks: list[Callable[[], Awaitable[None] | None]] = field(
        default_factory=list
    )


async def _run_shutdown_hooks(  # type: ignore[reportUnusedFunction]
    config: ModalWorkerConfig,
) -> None:
    """Run registered shutdown hooks in order and log individual failures."""
    for fn in config.shutdown_hooks:
        try:
            result = fn()
            if asyncio.iscoroutine(result):
                await result
        except Exception as err:
            logger.error("shutdown hook error: %s", err)
