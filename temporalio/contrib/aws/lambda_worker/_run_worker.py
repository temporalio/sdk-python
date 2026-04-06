from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import temporalio.client
import temporalio.worker
from temporalio.client import ClientConnectConfig
from temporalio.common import WorkerDeploymentVersion
from temporalio.contrib.aws.lambda_worker._configure import (
    LambdaWorkerConfig,
    _run_shutdown_hooks,
)
from temporalio.contrib.aws.lambda_worker._defaults import (
    DEFAULT_SHUTDOWN_HOOK_BUFFER,
    apply_lambda_worker_defaults,
    build_lambda_identity,
    lambda_default_config_file_path,
)
from temporalio.envconfig import ClientConfigProfile
from temporalio.worker import WorkerConfig, WorkerDeploymentConfig

logger = logging.getLogger(__name__)


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
    extract_lambda_ctx: Callable[[Any], tuple[str, str] | None] | None = None


def _default_load_config(getenv: Callable[[str], str | None]) -> ClientConfigProfile:
    config_path = lambda_default_config_file_path(getenv)  # type: ignore[arg-type]
    return ClientConfigProfile.load(config_source=config_path)


def _default_extract_lambda_ctx(
    lambda_context: Any,
) -> tuple[str, str] | None:
    """Extract (request_id, function_arn) from a Lambda context object."""
    if lambda_context is None:
        return None
    request_id = getattr(lambda_context, "aws_request_id", None)
    function_arn = getattr(lambda_context, "invoked_function_arn", None)
    if request_id is not None and function_arn is not None:
        return (request_id, function_arn)
    return None


def run_worker(
    version: WorkerDeploymentVersion,
    configure: Callable[[LambdaWorkerConfig], None],
) -> Callable[[Any, Any], None]:
    """Create a Temporal worker Lambda handler.

    Calls the *configure* callback to collect workflow/activity registrations and option overrides,
    then returns a Lambda handler function. On each invocation the handler connects to the Temporal
    server, starts a worker with Lambda-tuned defaults, polls for tasks until the invocation
    deadline approaches, and then gracefully shuts down.

    The *version* parameter identifies this worker's deployment version. ``run_worker`` always
    enables Worker Deployment Versioning (``use_worker_versioning=True``). To provide a default
    versioning behavior for workflows that do not specify one at registration time, set
    ``deployment_config`` in ``worker_config`` in the configure callback.

    The returned handler has the signature ``handler(event, context)`` and should be set as your
    Lambda function's handler entry point.

    Args:
        version: The worker deployment version. Required.
        configure: A callback that receives a :py:class:`LambdaWorkerConfig`
            (pre-populated with Lambda defaults) and configures workflows,
            activities, and options on it.

    Returns:
        A Lambda handler function.

    Example::

        from temporalio.common import WorkerDeploymentVersion
        from temporalio.contrib.aws.lambda_worker import (
            LambdaWorkerConfig,
            run_worker,
        )

        def configure(config: LambdaWorkerConfig) -> None:
            config.worker_config["task_queue"] = "my-task-queue"
            config.worker_config["workflows"] = [MyWorkflow]
            config.worker_config["activities"] = [my_activity]

        lambda_handler = run_worker(
            WorkerDeploymentVersion(
                deployment_name="my-service",
                build_id="v1.0",
            ),
            configure,
        )
    """
    deps = _WorkerDeps()
    try:
        return _run_worker_internal(version, configure, deps)
    except Exception as e:
        logger.error(f"fatal error running lambda worker: {e}")
        sys.exit(1)


def _run_worker_internal(
    version: WorkerDeploymentVersion,
    configure: Callable[[LambdaWorkerConfig], None],
    deps: _WorkerDeps,
) -> Callable[[Any, Any], None]:
    """Core logic with injected dependencies for testability."""
    if not version.deployment_name or not version.build_id:
        raise ValueError(
            "version is required (deployment_name and build_id must be set)"
        )

    # Load client config from envconfig / TOML.
    load_config = deps.load_config or (lambda: _default_load_config(deps.getenv))
    profile = load_config()
    connect_config: ClientConnectConfig = {**profile.to_client_connect_config()}

    # Build worker config with Lambda defaults.
    worker_config: WorkerConfig = {}
    apply_lambda_worker_defaults(worker_config)

    # Always enable deployment versioning.
    worker_config["deployment_config"] = WorkerDeploymentConfig(
        version=version,
        use_worker_versioning=True,
    )

    # Calculate default shutdown buffer.
    graceful_timeout = worker_config.get(
        "graceful_shutdown_timeout", timedelta(seconds=5)
    )
    shutdown_buffer = graceful_timeout + DEFAULT_SHUTDOWN_HOOK_BUFFER

    # Pre-populate config with defaults.
    config = LambdaWorkerConfig(
        client_connect_config=connect_config,
        worker_config=worker_config,
        shutdown_deadline_buffer=shutdown_buffer,
    )

    # Pre-populate task queue from environment if available.
    env_tq = deps.getenv("TEMPORAL_TASK_QUEUE")
    if env_tq:
        config.worker_config["task_queue"] = env_tq

    # Call user configure callback with pre-populated config.
    configure(config)

    # Validate task queue.
    if not config.worker_config.get("task_queue"):
        raise ValueError(
            "task queue not configured: set "
            'worker_config["task_queue"] or the '
            "TEMPORAL_TASK_QUEUE environment variable"
        )

    extract_lambda_ctx = deps.extract_lambda_ctx or _default_extract_lambda_ctx

    def _handler(_event: Any, lambda_context: Any) -> None:
        asyncio.run(
            _invocation_handler(
                lambda_context=lambda_context,
                config=config,
                deps=deps,
                extract_lambda_ctx=extract_lambda_ctx,
            )
        )

    return _handler


async def _invocation_handler(
    *,
    lambda_context: Any,
    config: LambdaWorkerConfig,
    deps: _WorkerDeps,
    extract_lambda_ctx: Callable[[Any], tuple[str, str] | None],
) -> None:
    """Handle a single Lambda invocation."""
    shutdown_buffer = config.shutdown_deadline_buffer

    # Check deadline feasibility.
    remaining_ms_fn = getattr(lambda_context, "get_remaining_time_in_millis", None)
    deadline_available = remaining_ms_fn is not None
    if deadline_available:
        assert remaining_ms_fn is not None
        remaining = timedelta(milliseconds=remaining_ms_fn())
        work_time = remaining - shutdown_buffer
        if work_time <= timedelta(seconds=1):
            raise RuntimeError(
                f"Lambda timeout is too short: {remaining.total_seconds():.1f}s "
                f"remaining but {shutdown_buffer.total_seconds():.1f}s is "
                f"reserved for shutdown, leaving no time for work. "
                f"Increase the function timeout or decrease the shutdown "
                f"deadline buffer"
            )
        elif work_time < timedelta(seconds=5):
            logger.warning(
                "Lambda timeout leaves less than 5s for work after "
                "shutdown buffer; consider increasing the function "
                "timeout or decreasing the shutdown deadline buffer "
                "(work_time=%s, shutdown_buffer=%s)",
                work_time,
                shutdown_buffer,
            )

    # Build per-invocation connect kwargs with identity from Lambda context.
    invocation_connect_kwargs: ClientConnectConfig = {**config.client_connect_config}
    if "identity" not in invocation_connect_kwargs:
        ctx_info = extract_lambda_ctx(lambda_context)
        if ctx_info is not None:
            request_id, function_arn = ctx_info
            invocation_connect_kwargs["identity"] = build_lambda_identity(
                request_id, function_arn
            )

    # Connect to Temporal.
    client = await deps.connect(**invocation_connect_kwargs)

    # Create the worker.
    worker = deps.create_worker(client, **config.worker_config)

    # Run the worker until the deadline approaches or context is done.
    if deadline_available:
        assert remaining_ms_fn is not None
        work_time_secs = (
            timedelta(milliseconds=remaining_ms_fn()) - shutdown_buffer
        ).total_seconds()
        if work_time_secs > 0:
            try:
                await asyncio.wait_for(worker.run(), timeout=work_time_secs)
            except asyncio.TimeoutError:
                pass
    else:
        # No deadline - run until cancelled.
        await worker.run()

    # Run shutdown hooks after worker has stopped.
    await _run_shutdown_hooks(config)
