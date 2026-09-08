from __future__ import annotations

import asyncio
import inspect
import logging
import os
import sys
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, TypeAlias

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

# A plain, ``async def`` coroutine, ``async def`` generator, or async-context-manager
# callback. See run_worker.
ConfigureCallback: TypeAlias = Callable[
    [LambdaWorkerConfig],
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


def _validate_task_queue(config: LambdaWorkerConfig) -> None:
    """Raise if no task queue has been configured."""
    if not config.worker_config.get("task_queue"):
        raise ValueError(
            "task queue not configured: set "
            'worker_config["task_queue"] or the '
            "TEMPORAL_TASK_QUEUE environment variable"
        )


def run_worker(
    version: WorkerDeploymentVersion,
    configure: ConfigureCallback,
) -> Callable[[Any, Any], None]:
    """Create a Temporal worker Lambda handler.

    Calls the *configure* callback to collect workflow/activity registrations and option
    overrides, then returns a Lambda handler function. On each invocation the handler
    connects to the Temporal server, starts a worker with Lambda-tuned defaults, polls for
    tasks until the invocation deadline approaches, and then gracefully shuts down.

    The *configure* callback is invoked **once per invocation** and may be synchronous or
    asynchronous:

    * **Synchronous** ``def configure(config) -> None`` — runs per invocation. Use for
      static worker definition (task queue, registrations, option tuning) and resources
      that are not bound to an event loop.
    * **Async** ``async def configure(config) -> None`` — awaited per invocation. Use when
      setup must ``await`` (for example, opening an async client). Pair with
      ``shutdown_hooks`` for teardown.
    * **Async generator** ``async def configure(config): ...; yield; ...`` (or an
      equivalent ``@contextlib.asynccontextmanager``-decorated function) — entered per
      invocation. Statements before the single ``yield`` run before the client connects;
      the worker runs while the generator is suspended at the ``yield``; statements after
      the ``yield`` run as teardown once the worker has stopped. Any ``shutdown_hooks``
      registered before the ``yield`` run after the worker stops but *before* the
      post-``yield`` teardown, so this resource outlives the hooks (e.g. a telemetry
      flush hook can still emit before the resource is closed). This is the recommended
      shape for event-loop-bound resources that must live for the duration of the
      invocation, such as an ``aioboto3`` S3 client backing the external-storage data
      converter (see the async example below).

    The callback runs per invocation (not once at cold start) because event-loop-bound
    resources cannot be created at cold start (there is no running loop) and cannot be
    shared across invocations (each invocation runs under a fresh ``asyncio.run`` loop).

    The *version* parameter identifies this worker's deployment version. ``run_worker``
    always enables Worker Deployment Versioning (``use_worker_versioning=True``). To
    provide a default versioning behavior for workflows that do not specify one at
    registration time, set ``deployment_config`` in ``worker_config`` in the configure
    callback.

    The returned handler has the signature ``handler(event, context)`` and should be set as
    your Lambda function's handler entry point.

    Args:
        version: The worker deployment version. Required.
        configure: A callback that receives a :py:class:`LambdaWorkerConfig`
            (pre-populated with Lambda defaults) and configures workflows,
            activities, and options on it. May be sync, async, or an async
            generator (see above).

    Returns:
        A Lambda handler function.

    Example:
        Synchronous configure (static worker definition)::

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
                    build_id="v1.0"),
                configure,
            )

        Async generator configure, bracketing an ``aioboto3`` S3 client. The session
        lives at module scope (it is not event-loop-bound and caches credentials across
        warm invocations); only the loop-bound client is opened per invocation::

            import aioboto3
            import dataclasses
            from temporalio.contrib.aws.s3driver import S3StorageDriver
            from temporalio.contrib.aws.s3driver.aioboto3 import new_aioboto3_client
            from temporalio.converter import DataConverter, ExternalStorage

            session = aioboto3.Session()

            async def configure(config: LambdaWorkerConfig):
                config.worker_config["task_queue"] = "my-task-queue"
                config.worker_config["workflows"] = [MyWorkflow]
                async with session.client("s3") as s3_client:
                    driver = S3StorageDriver(
                        client=new_aioboto3_client(s3_client), bucket="my-payloads",
                    )
                    config.client_connect_config["data_converter"] = dataclasses.replace(
                        DataConverter.default,
                        external_storage=ExternalStorage(drivers=[driver]),
                    )
                    yield

            lambda_handler = run_worker(
                WorkerDeploymentVersion(
                    deployment_name="my-service",
                    build_id="v1.0"),
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
    configure: ConfigureCallback,
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
    base_connect_config: ClientConnectConfig = {**profile.to_client_connect_config()}

    # Build base worker config with Lambda defaults.
    base_worker_config: WorkerConfig = {}
    apply_lambda_worker_defaults(base_worker_config)

    # Always enable deployment versioning.
    base_worker_config["deployment_config"] = WorkerDeploymentConfig(
        version=version,
        use_worker_versioning=True,
    )

    # Calculate default shutdown buffer.
    graceful_timeout = base_worker_config.get(
        "graceful_shutdown_timeout", timedelta(seconds=5)
    )
    shutdown_buffer = graceful_timeout + DEFAULT_SHUTDOWN_HOOK_BUFFER

    env_tq = deps.getenv("TEMPORAL_TASK_QUEUE")

    def _new_config() -> LambdaWorkerConfig:
        """Fresh config per invocation; dicts/hooks are copied so nothing leaks across
        invocations.
        """
        config = LambdaWorkerConfig(
            client_connect_config={**base_connect_config},
            worker_config={**base_worker_config},
            shutdown_deadline_buffer=shutdown_buffer,
        )
        if env_tq:
            config.worker_config["task_queue"] = env_tq
        return config

    extract_lambda_ctx = deps.extract_lambda_ctx or _default_extract_lambda_ctx

    def _handler(_event: Any, lambda_context: Any) -> None:
        asyncio.run(
            _invocation_handler(
                lambda_context=lambda_context,
                configure=configure,
                new_config=_new_config,
                deps=deps,
                extract_lambda_ctx=extract_lambda_ctx,
            )
        )

    return _handler


@asynccontextmanager
async def _invocation_config_scope(
    configure: ConfigureCallback,
    new_config: Callable[[], LambdaWorkerConfig],
) -> AsyncGenerator[LambdaWorkerConfig, None]:
    """Run *configure* (see run_worker for the forms) against a fresh per-invocation config
    and yield it. For the generator / context-manager forms, post-``yield`` teardown runs
    when the caller's block exits, including on error. Task queue is validated after
    setup.
    """
    config = new_config()
    if inspect.isasyncgenfunction(configure):
        # Wrap the bare async generator so it drives like a context manager: setup on
        # enter, teardown on exit.
        cm: Any = asynccontextmanager(configure)(config)
    else:
        result = configure(config)
        if inspect.isawaitable(result):
            await result
        # A @asynccontextmanager-decorated callback returns the context manager directly.
        cm = result if result is not None and hasattr(result, "__aenter__") else None

    if cm is not None:
        async with cm:
            _validate_task_queue(config)
            yield config
    else:
        _validate_task_queue(config)
        yield config


async def _invocation_handler(
    *,
    lambda_context: Any,
    configure: ConfigureCallback,
    new_config: Callable[[], LambdaWorkerConfig],
    deps: _WorkerDeps,
    extract_lambda_ctx: Callable[[Any], tuple[str, str] | None],
) -> None:
    """Handle a single Lambda invocation."""
    async with _invocation_config_scope(configure, new_config) as config:
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
        invocation_connect_kwargs: ClientConnectConfig = {
            **config.client_connect_config
        }
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

        # Run shutdown hooks after worker has stopped, before any async-generator
        # configure teardown (which unwinds on scope exit).
        await _run_shutdown_hooks(config)
