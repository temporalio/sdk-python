from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import os
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, TypeAlias

import temporalio.client
import temporalio.worker
from temporalio.client import ClientConnectConfig
from temporalio.common import WorkerDeploymentVersion
from temporalio.contrib.modal.worker._configure import (
    ModalWorkerConfig,
    _run_shutdown_hooks,
)
from temporalio.contrib.modal.worker._defaults import (
    DEFAULT_EXECUTION_TIMEOUT,
    DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT,
    DEFAULT_SHUTDOWN_HOOK_BUFFER,
    ENV_MODAL_TASK_ID,
    ENV_TASK_QUEUE,
    MIN_FUNCTION_TIMEOUT_HEADROOM,
    apply_modal_worker_defaults,
    build_modal_identity,
    modal_default_config_file_path,
)
from temporalio.envconfig import ClientConfigProfile
from temporalio.worker import WorkerConfig, WorkerDeploymentConfig

logger = logging.getLogger(__name__)

ConfigureCallback: TypeAlias = Callable[
    [ModalWorkerConfig],
    None
    | Awaitable[None]
    | AsyncGenerator[None, None]
    | AbstractAsyncContextManager[None],
]


@dataclass
class _WorkerDeps:
    """External dependencies injected for testability."""

    connect: Callable[..., Awaitable[temporalio.client.Client]] = field(
        default_factory=lambda: temporalio.client.Client.connect
    )
    create_worker: Callable[..., temporalio.worker.Worker] = field(
        default_factory=lambda: temporalio.worker.Worker
    )
    load_config: Callable[[], ClientConfigProfile] | None = None
    getenv: Callable[[str], str | None] = field(default_factory=lambda: os.environ.get)
    get_function_call_id: Callable[[], str] | None = None
    monotonic: Callable[[], float] = time.monotonic


def _default_load_config(getenv: Callable[[str], str | None]) -> ClientConfigProfile:
    config_path = modal_default_config_file_path(getenv)
    return ClientConfigProfile.load(config_source=config_path)


def _default_get_function_call_id() -> str:
    try:
        modal = importlib.import_module("modal")
    except ImportError as err:
        raise RuntimeError(
            "the Modal Python package is required to run a Modal worker"
        ) from err
    return modal.current_function_call_id()


def _validate_version(version: WorkerDeploymentVersion) -> None:
    if not version.deployment_name or not version.build_id:
        raise ValueError(
            "version is required (deployment_name and build_id must be set)"
        )


def _validate_task_queue(config: ModalWorkerConfig) -> None:
    if not config.worker_config.get("task_queue"):
        raise ValueError(
            "task queue not configured: set "
            'worker_config["task_queue"] or the '
            "TEMPORAL_TASK_QUEUE environment variable"
        )


def _work_time(
    config: ModalWorkerConfig, invocation_started: float, monotonic: Callable[[], float]
) -> timedelta:
    if config.execution_timeout <= timedelta(0):
        raise ValueError("execution_timeout must be greater than zero")
    if config.shutdown_deadline_buffer < timedelta(0):
        raise ValueError("shutdown_deadline_buffer must not be negative")
    elapsed = timedelta(seconds=monotonic() - invocation_started)
    return config.execution_timeout - elapsed - config.shutdown_deadline_buffer


def _validate_function_timeout(
    config: ModalWorkerConfig, modal_function_timeout: timedelta
) -> None:
    if modal_function_timeout <= timedelta(0):
        raise ValueError("modal_function_timeout must be greater than zero")
    minimum_function_timeout = config.execution_timeout + MIN_FUNCTION_TIMEOUT_HEADROOM
    if modal_function_timeout < minimum_function_timeout:
        raise ValueError(
            "modal_function_timeout must be at least execution_timeout + "
            f"{MIN_FUNCTION_TIMEOUT_HEADROOM.total_seconds():.0f}s "
            f"({minimum_function_timeout.total_seconds():.1f}s required, "
            f"{modal_function_timeout.total_seconds():.1f}s configured)"
        )


async def _run_worker_until_timeout(
    worker: temporalio.worker.Worker, timeout: float
) -> None:
    """Run a worker, suppressing only the timeout imposed by this function."""
    worker_raised_timeout = False

    async def tracked_run() -> None:
        nonlocal worker_raised_timeout
        try:
            await worker.run()
        except asyncio.TimeoutError:
            # Python 3.11 aliases asyncio.TimeoutError to TimeoutError, so track
            # whether the worker or wait_for raised it.
            worker_raised_timeout = True
            raise

    try:
        await asyncio.wait_for(tracked_run(), timeout=timeout)
    except asyncio.TimeoutError:
        if worker_raised_timeout:
            raise


async def run_worker(
    version: WorkerDeploymentVersion,
    configure: ConfigureCallback,
    *,
    modal_function_timeout: timedelta,
) -> None:
    """Run a Temporal worker for one Modal Function invocation.

    Call this from a top-level async function decorated with
    ``@app.function``. A fresh Temporal client and worker are created for every
    invocation. The worker polls until its execution budget approaches the
    configured shutdown buffer, then drains and runs shutdown hooks.

    The callback may be synchronous, async, an async generator, or an async
    context manager. Async-generator setup runs before the Temporal client
    connects; its teardown runs after the worker and shutdown hooks stop.

    Worker Deployment Versioning is always enabled. Modal does not expose a
    remaining-time API inside a running Function, so *modal_function_timeout*
    must declare the timeout configured on ``@app.function``. The declared
    timeout must be at least
    :py:attr:`ModalWorkerConfig.execution_timeout` plus
    :py:const:`MIN_FUNCTION_TIMEOUT_HEADROOM`. The SDK validates that
    relationship but cannot verify the decorator configuration itself.

    Args:
        version: Required worker deployment version.
        configure: Per-invocation callback that registers workflows and
            activities and may override client or worker options.
        modal_function_timeout: Timeout configured on the enclosing Modal
            Function. The deployment and this argument must use the same value.

    Example::

        from datetime import timedelta

        import modal
        from temporalio.common import WorkerDeploymentVersion
        from temporalio.contrib.modal.worker import (
            MIN_FUNCTION_TIMEOUT_HEADROOM,
            ModalWorkerConfig,
            run_worker,
        )

        app = modal.App("temporal-worker")
        worker_execution_timeout = timedelta(minutes=5)
        modal_function_timeout = (
            worker_execution_timeout + MIN_FUNCTION_TIMEOUT_HEADROOM
        )

        def configure(config: ModalWorkerConfig) -> None:
            config.worker_config["task_queue"] = "my-task-queue"
            config.worker_config["workflows"] = [MyWorkflow]
            config.worker_config["activities"] = [my_activity]
            config.execution_timeout = worker_execution_timeout

        @app.function(timeout=int(modal_function_timeout.total_seconds()))
        async def temporal_worker() -> None:
            await run_worker(
                WorkerDeploymentVersion(
                    deployment_name="my-service",
                    build_id="v1.0",
                ),
                configure,
                modal_function_timeout=modal_function_timeout,
            )
    """
    await _run_worker_internal(
        version,
        configure,
        modal_function_timeout,
        _WorkerDeps(),
    )


async def _run_worker_internal(
    version: WorkerDeploymentVersion,
    configure: ConfigureCallback,
    modal_function_timeout: timedelta,
    deps: _WorkerDeps,
) -> None:
    """Run one invocation with injected dependencies."""
    invocation_started = deps.monotonic()
    _validate_version(version)

    load_config = deps.load_config or (lambda: _default_load_config(deps.getenv))
    profile = load_config()
    base_connect_config: ClientConnectConfig = {**profile.to_client_connect_config()}

    base_worker_config: WorkerConfig = {}
    apply_modal_worker_defaults(base_worker_config)
    base_worker_config["deployment_config"] = WorkerDeploymentConfig(
        version=version,
        use_worker_versioning=True,
    )

    graceful_timeout = base_worker_config.get(
        "graceful_shutdown_timeout", DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT
    )
    shutdown_buffer = graceful_timeout + DEFAULT_SHUTDOWN_HOOK_BUFFER
    config = ModalWorkerConfig(
        client_connect_config=base_connect_config,
        worker_config=base_worker_config,
        execution_timeout=DEFAULT_EXECUTION_TIMEOUT,
        shutdown_deadline_buffer=shutdown_buffer,
    )
    env_task_queue = deps.getenv(ENV_TASK_QUEUE)
    if env_task_queue:
        config.worker_config["task_queue"] = env_task_queue

    async with _invocation_config_scope(configure, config):
        try:
            work_time = _work_time(config, invocation_started, deps.monotonic)
            _validate_function_timeout(config, modal_function_timeout)
            if work_time <= timedelta(seconds=1):
                raise RuntimeError(
                    "Modal execution timeout is too short: "
                    f"{config.execution_timeout.total_seconds():.1f}s configured but "
                    f"{config.shutdown_deadline_buffer.total_seconds():.1f}s is "
                    "reserved for shutdown, leaving no time for work after setup. "
                    "Increase execution_timeout (and the Modal Function timeout if "
                    "necessary), or decrease the shutdown deadline buffer"
                )
            if work_time < timedelta(seconds=5):
                logger.warning(
                    "Modal execution timeout leaves less than 5s for work after setup "
                    "and the shutdown buffer; consider increasing execution_timeout "
                    "(and the Modal Function timeout if necessary) or decreasing the "
                    "shutdown deadline buffer (work_time=%s, shutdown_buffer=%s)",
                    work_time,
                    config.shutdown_deadline_buffer,
                )

            invocation_connect_config: ClientConnectConfig = {
                **config.client_connect_config
            }
            if "identity" not in invocation_connect_config:
                get_function_call_id = (
                    deps.get_function_call_id or _default_get_function_call_id
                )
                invocation_connect_config["identity"] = build_modal_identity(
                    get_function_call_id(), deps.getenv(ENV_MODAL_TASK_ID) or ""
                )

            client = await deps.connect(**invocation_connect_config)
            worker = deps.create_worker(client, **config.worker_config)

            work_time = _work_time(config, invocation_started, deps.monotonic)
            if work_time <= timedelta(0):
                raise RuntimeError(
                    "Modal execution budget was exhausted before the worker started"
                )
            await _run_worker_until_timeout(worker, work_time.total_seconds())
        finally:
            await _run_shutdown_hooks(config)


@asynccontextmanager
async def _invocation_config_scope(
    configure: ConfigureCallback,
    config: ModalWorkerConfig,
) -> AsyncGenerator[ModalWorkerConfig, None]:
    """Run configure setup and bracket any async teardown around the worker."""
    if inspect.isasyncgenfunction(configure):
        cm: Any = asynccontextmanager(configure)(config)
    else:
        result = configure(config)
        if inspect.isawaitable(result):
            await result
        cm = result if result is not None and hasattr(result, "__aenter__") else None

    if cm is not None:
        async with cm:
            _validate_task_queue(config)
            yield config
    else:
        _validate_task_queue(config)
        yield config
