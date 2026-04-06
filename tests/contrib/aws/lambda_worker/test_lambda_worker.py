"""Tests for temporalio.contrib.aws.lambda_worker."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from temporalio.common import WorkerDeploymentVersion
from temporalio.contrib.aws.lambda_worker._configure import (
    LambdaWorkerConfig,
    _run_shutdown_hooks,
)
from temporalio.contrib.aws.lambda_worker._defaults import (
    DEFAULT_MAX_CACHED_WORKFLOWS,
    DEFAULT_MAX_CONCURRENT_ACTIVITIES,
    DEFAULT_MAX_CONCURRENT_LOCAL_ACTIVITIES,
    DEFAULT_MAX_CONCURRENT_NEXUS_TASKS,
    DEFAULT_MAX_CONCURRENT_WORKFLOW_TASKS,
    apply_lambda_worker_defaults,
    build_lambda_identity,
    lambda_default_config_file_path,
)
from temporalio.contrib.aws.lambda_worker._run_worker import (
    _run_worker_internal,
    _WorkerDeps,
)
from temporalio.envconfig import ClientConfigProfile
from temporalio.worker import WorkerConfig

TEST_VERSION = WorkerDeploymentVersion(
    deployment_name="test-deployment",
    build_id="test-build",
)


# ---- LambdaWorkerConfig tests ----


class TestLambdaWorkerConfig:
    def test_worker_config_task_queue(self) -> None:
        config = LambdaWorkerConfig()
        assert config.worker_config.get("task_queue") is None
        config.worker_config["task_queue"] = "my-queue"
        assert config.worker_config["task_queue"] == "my-queue"

    def test_worker_config_workflows(self) -> None:
        config = LambdaWorkerConfig()

        class FakeWorkflow:
            pass

        config.worker_config["workflows"] = [FakeWorkflow]
        assert FakeWorkflow in config.worker_config["workflows"]

    def test_worker_config_activities(self) -> None:
        config = LambdaWorkerConfig()

        def fake_activity() -> None:
            pass

        config.worker_config["activities"] = [fake_activity]
        assert fake_activity in config.worker_config["activities"]

    def test_client_connect_config_directly_modifiable(self) -> None:
        config = LambdaWorkerConfig()
        config.client_connect_config["namespace"] = "custom-ns"
        assert config.client_connect_config["namespace"] == "custom-ns"

    def test_worker_config_directly_modifiable(self) -> None:
        config = LambdaWorkerConfig()
        config.worker_config["max_concurrent_activities"] = 42
        assert config.worker_config["max_concurrent_activities"] == 42

    def test_shutdown_deadline_buffer(self) -> None:
        config = LambdaWorkerConfig()
        config.shutdown_deadline_buffer = timedelta(seconds=5)
        assert config.shutdown_deadline_buffer == timedelta(seconds=5)

    def test_shutdown_hooks_list(self) -> None:
        config = LambdaWorkerConfig()
        fn = MagicMock()
        config.shutdown_hooks.append(fn)
        assert fn in config.shutdown_hooks

    @pytest.mark.asyncio
    async def test_run_shutdown_hooks_in_order(self) -> None:
        config = LambdaWorkerConfig()
        order: list[str] = []
        config.shutdown_hooks.append(lambda: order.append("first"))
        config.shutdown_hooks.append(lambda: order.append("second"))
        await _run_shutdown_hooks(config)
        assert order == ["first", "second"]

    @pytest.mark.asyncio
    async def test_run_shutdown_hooks_async(self) -> None:
        config = LambdaWorkerConfig()
        called = False

        async def async_hook() -> None:
            nonlocal called
            called = True

        config.shutdown_hooks.append(async_hook)
        await _run_shutdown_hooks(config)
        assert called

    @pytest.mark.asyncio
    async def test_run_shutdown_hooks_error_continues(self) -> None:
        config = LambdaWorkerConfig()
        second_called = False

        def failing_hook() -> None:
            raise RuntimeError("flush failed")

        def second_hook() -> None:
            nonlocal second_called
            second_called = True

        config.shutdown_hooks.append(failing_hook)
        config.shutdown_hooks.append(second_hook)
        await _run_shutdown_hooks(config)
        assert second_called

    def test_is_dataclass(self) -> None:
        import dataclasses

        assert dataclasses.is_dataclass(LambdaWorkerConfig)

    def test_default_field_independence(self) -> None:
        """Each instance gets its own mutable containers."""
        a = LambdaWorkerConfig()
        b = LambdaWorkerConfig()
        a.worker_config["max_concurrent_activities"] = 99
        assert "max_concurrent_activities" not in b.worker_config


# ---- Defaults tests ----


class TestDefaults:
    def test_apply_lambda_worker_defaults(self) -> None:
        config: WorkerConfig = {}
        apply_lambda_worker_defaults(config)
        assert (
            config.get("max_concurrent_activities") == DEFAULT_MAX_CONCURRENT_ACTIVITIES
        )
        assert (
            config.get("max_concurrent_workflow_tasks")
            == DEFAULT_MAX_CONCURRENT_WORKFLOW_TASKS
        )
        assert (
            config.get("max_concurrent_local_activities")
            == DEFAULT_MAX_CONCURRENT_LOCAL_ACTIVITIES
        )
        assert (
            config.get("max_concurrent_nexus_tasks")
            == DEFAULT_MAX_CONCURRENT_NEXUS_TASKS
        )
        assert config.get("max_cached_workflows") == DEFAULT_MAX_CACHED_WORKFLOWS
        assert config.get("disable_eager_activity_execution") is True

    def test_apply_lambda_worker_defaults_preserves_existing(self) -> None:
        config: WorkerConfig = {
            "max_concurrent_activities": 50,
            "graceful_shutdown_timeout": timedelta(seconds=10),
        }
        apply_lambda_worker_defaults(config)
        assert config.get("max_concurrent_activities") == 50
        assert config.get("graceful_shutdown_timeout") == timedelta(seconds=10)
        assert config.get("disable_eager_activity_execution") is True

    def test_build_lambda_identity(self) -> None:
        assert (
            build_lambda_identity("req-123", "arn:aws:lambda:us-east-1:123:function:f")
            == "req-123@arn:aws:lambda:us-east-1:123:function:f"
        )

    def test_build_lambda_identity_empty(self) -> None:
        assert build_lambda_identity("", "") == "unknown@unknown"

    def test_lambda_default_config_file_path_env_var(self) -> None:
        env = {"TEMPORAL_CONFIG_FILE": "/custom/path.toml"}
        assert (
            lambda_default_config_file_path(env.get)  # type: ignore[arg-type]
            == Path("/custom/path.toml")
        )

    def test_lambda_default_config_file_path_lambda_root(self) -> None:
        env = {"LAMBDA_TASK_ROOT": "/var/task"}
        assert (
            lambda_default_config_file_path(env.get)  # type: ignore[arg-type]
            == Path("/var/task/temporal.toml")
        )

    def test_lambda_default_config_file_path_cwd(self) -> None:
        env: dict[str, str] = {}
        assert (
            lambda_default_config_file_path(env.get)  # type: ignore[arg-type]
            == Path("temporal.toml")
        )


# ---- RunWorker tests ----


def _make_lambda_context(
    *,
    remaining_ms: int = 3_600_000,
    request_id: str = "req-123",
    function_arn: str = "arn:aws:lambda:us-east-1:123:function:my-func",
) -> Any:
    """Create a mock Lambda context object."""
    ctx = MagicMock()
    ctx.get_remaining_time_in_millis.return_value = remaining_ms
    ctx.aws_request_id = request_id
    ctx.invoked_function_arn = function_arn
    return ctx


def _make_test_deps(
    *,
    connect_kwargs_capture: list[dict[str, Any]] | None = None,
    worker_kwargs_capture: list[dict[str, Any]] | None = None,
) -> _WorkerDeps:
    """Create test deps with mocked connect and worker."""
    mock_client = MagicMock()
    mock_worker = MagicMock()
    mock_worker.run = AsyncMock()

    async def fake_connect(**kwargs: Any) -> Any:
        if connect_kwargs_capture is not None:
            connect_kwargs_capture.append(kwargs)
        return mock_client

    def fake_create_worker(_client: Any, **kwargs: Any) -> Any:
        if worker_kwargs_capture is not None:
            worker_kwargs_capture.append(kwargs)
        return mock_worker

    return _WorkerDeps(
        connect=fake_connect,
        create_worker=fake_create_worker,
        load_config=lambda: ClientConfigProfile(),
        getenv={"TEMPORAL_TASK_QUEUE": "test-queue"}.get,  # type: ignore[arg-type]
        extract_lambda_ctx=lambda ctx: (
            ctx.aws_request_id,
            ctx.invoked_function_arn,
        )
        if hasattr(ctx, "aws_request_id")
        else None,
    )


class TestRunWorkerInternal:
    def test_returns_handler(self) -> None:
        deps = _make_test_deps()
        handler = _run_worker_internal(TEST_VERSION, lambda config: None, deps)
        assert callable(handler)

    def test_success(self) -> None:
        deps = _make_test_deps()

        def configure(config: LambdaWorkerConfig) -> None:
            config.worker_config["workflows"] = [type("FakeWf", (), {})]

        handler = _run_worker_internal(TEST_VERSION, configure, deps)
        handler({}, _make_lambda_context())

    def test_configure_callback_error(self) -> None:
        deps = _make_test_deps()

        def bad_configure(_config: LambdaWorkerConfig) -> None:
            raise RuntimeError("bad config")

        with pytest.raises(RuntimeError, match="bad config"):
            _run_worker_internal(TEST_VERSION, bad_configure, deps)

    def test_missing_task_queue(self) -> None:
        deps = _make_test_deps()
        deps.getenv = lambda _: None  # type: ignore[assignment]
        with pytest.raises(ValueError, match="task queue not configured"):
            _run_worker_internal(TEST_VERSION, lambda config: None, deps)

    def test_missing_version(self) -> None:
        deps = _make_test_deps()
        with pytest.raises(ValueError, match="version is required"):
            _run_worker_internal(
                WorkerDeploymentVersion(deployment_name="", build_id=""),
                lambda config: None,
                deps,
            )

    def test_user_overrides_applied(self) -> None:
        connect_capture: list[dict[str, Any]] = []
        worker_capture: list[dict[str, Any]] = []
        deps = _make_test_deps(
            connect_kwargs_capture=connect_capture,
            worker_kwargs_capture=worker_capture,
        )

        def configure(config: LambdaWorkerConfig) -> None:
            config.worker_config["task_queue"] = "user-queue"
            config.client_connect_config["namespace"] = "custom-ns"
            config.worker_config["max_concurrent_activities"] = 99

        handler = _run_worker_internal(TEST_VERSION, configure, deps)
        handler({}, _make_lambda_context())

        assert connect_capture[0]["namespace"] == "custom-ns"
        assert worker_capture[0]["max_concurrent_activities"] == 99

    def test_lambda_defaults_applied(self) -> None:
        worker_capture: list[dict[str, Any]] = []
        deps = _make_test_deps(worker_kwargs_capture=worker_capture)
        handler = _run_worker_internal(TEST_VERSION, lambda config: None, deps)
        handler({}, _make_lambda_context())

        kwargs = worker_capture[0]
        assert kwargs["max_concurrent_activities"] == DEFAULT_MAX_CONCURRENT_ACTIVITIES
        assert (
            kwargs["max_concurrent_workflow_tasks"]
            == DEFAULT_MAX_CONCURRENT_WORKFLOW_TASKS
        )
        assert kwargs["disable_eager_activity_execution"] is True
        dc = kwargs["deployment_config"]
        assert dc.use_worker_versioning is True
        assert dc.version == TEST_VERSION

    def test_identity_from_lambda_context(self) -> None:
        connect_capture: list[dict[str, Any]] = []
        deps = _make_test_deps(connect_kwargs_capture=connect_capture)
        handler = _run_worker_internal(TEST_VERSION, lambda config: None, deps)
        handler(
            {},
            _make_lambda_context(
                request_id="req-abc-123",
                function_arn="arn:aws:lambda:us-east-1:123456:function:my-func",
            ),
        )

        assert (
            connect_capture[0]["identity"]
            == "req-abc-123@arn:aws:lambda:us-east-1:123456:function:my-func"
        )

    def test_identity_user_override_wins(self) -> None:
        connect_capture: list[dict[str, Any]] = []
        deps = _make_test_deps(connect_kwargs_capture=connect_capture)

        def configure(config: LambdaWorkerConfig) -> None:
            config.client_connect_config["identity"] = "my-custom-identity"

        handler = _run_worker_internal(TEST_VERSION, configure, deps)
        handler({}, _make_lambda_context())
        assert connect_capture[0]["identity"] == "my-custom-identity"

    def test_identity_no_lambda_context(self) -> None:
        connect_capture: list[dict[str, Any]] = []
        deps = _make_test_deps(connect_kwargs_capture=connect_capture)
        deps.extract_lambda_ctx = lambda ctx: None
        handler = _run_worker_internal(TEST_VERSION, lambda config: None, deps)
        handler({}, MagicMock(spec=[]))
        assert "identity" not in connect_capture[0]

    def test_shutdown_hooks_called(self) -> None:
        deps = _make_test_deps()
        shutdown_called = False

        def configure(config: LambdaWorkerConfig) -> None:
            def hook() -> None:
                nonlocal shutdown_called
                shutdown_called = True

            config.shutdown_hooks.append(hook)

        handler = _run_worker_internal(TEST_VERSION, configure, deps)
        handler({}, _make_lambda_context())
        assert shutdown_called

    def test_shutdown_hooks_called_per_invocation(self) -> None:
        deps = _make_test_deps()
        shutdown_count = 0

        def configure(config: LambdaWorkerConfig) -> None:
            def hook() -> None:
                nonlocal shutdown_count
                shutdown_count += 1

            config.shutdown_hooks.append(hook)

        handler = _run_worker_internal(TEST_VERSION, configure, deps)
        handler({}, _make_lambda_context())
        handler({}, _make_lambda_context())
        handler({}, _make_lambda_context())
        assert shutdown_count == 3

    def test_shutdown_hooks_multiple_funcs_order(self) -> None:
        deps = _make_test_deps()
        order: list[str] = []

        def configure(config: LambdaWorkerConfig) -> None:
            config.shutdown_hooks.append(lambda: order.append("first"))
            config.shutdown_hooks.append(lambda: order.append("second"))

        handler = _run_worker_internal(TEST_VERSION, configure, deps)
        handler({}, _make_lambda_context())
        assert order == ["first", "second"]

    def test_shutdown_hooks_error_continues(self) -> None:
        deps = _make_test_deps()
        second_called = False

        def configure(config: LambdaWorkerConfig) -> None:
            def failing() -> None:
                raise RuntimeError("flush failed")

            def second() -> None:
                nonlocal second_called
                second_called = True

            config.shutdown_hooks.append(failing)
            config.shutdown_hooks.append(second)

        handler = _run_worker_internal(TEST_VERSION, configure, deps)
        handler({}, _make_lambda_context())
        assert second_called

    def test_tight_deadline_raises_error(self) -> None:
        deps = _make_test_deps()

        def configure(config: LambdaWorkerConfig) -> None:
            config.shutdown_deadline_buffer = timedelta(milliseconds=1500)

        handler = _run_worker_internal(TEST_VERSION, configure, deps)
        with pytest.raises(RuntimeError, match="Lambda timeout is too short"):
            handler({}, _make_lambda_context(remaining_ms=2000))

    def test_tight_deadline_logs_warning(self) -> None:
        deps = _make_test_deps()

        def configure(config: LambdaWorkerConfig) -> None:
            config.shutdown_deadline_buffer = timedelta(milliseconds=500)

        handler = _run_worker_internal(TEST_VERSION, configure, deps)
        with patch(
            "temporalio.contrib.aws.lambda_worker._run_worker.logger"
        ) as mock_logger:
            handler({}, _make_lambda_context(remaining_ms=2000))
            mock_logger.warning.assert_called_once()
            assert "less than 5s" in mock_logger.warning.call_args[0][0]

    def test_per_invocation_lifecycle(self) -> None:
        """Each invocation creates its own client and worker."""
        connect_count = 0
        deps = _make_test_deps()
        original_connect = deps.connect

        async def counting_connect(**kwargs: Any) -> Any:
            nonlocal connect_count
            connect_count += 1
            return await original_connect(**kwargs)

        deps.connect = counting_connect

        handler = _run_worker_internal(TEST_VERSION, lambda config: None, deps)
        handler({}, _make_lambda_context())
        handler({}, _make_lambda_context())
        handler({}, _make_lambda_context())
        assert connect_count == 3

    def test_task_queue_from_config(self) -> None:
        worker_capture: list[dict[str, Any]] = []
        deps = _make_test_deps(worker_kwargs_capture=worker_capture)
        deps.getenv = lambda _: None  # type: ignore[assignment]

        def configure(config: LambdaWorkerConfig) -> None:
            config.worker_config["task_queue"] = "explicit-queue"

        handler = _run_worker_internal(TEST_VERSION, configure, deps)
        handler({}, _make_lambda_context())
        assert worker_capture[0]["task_queue"] == "explicit-queue"

    def test_task_queue_pre_populated_from_env(self) -> None:
        """Task queue is pre-populated from TEMPORAL_TASK_QUEUE env var."""
        deps = _make_test_deps()
        task_queues: list[str | None] = []

        def configure(config: LambdaWorkerConfig) -> None:
            task_queues.append(config.worker_config.get("task_queue"))

        _run_worker_internal(TEST_VERSION, configure, deps)
        assert task_queues[0] == "test-queue"

    def test_config_pre_populated_with_defaults(self) -> None:
        """Configure callback receives pre-populated LambdaWorkerConfig."""
        deps = _make_test_deps()
        captured: list[LambdaWorkerConfig] = []

        def configure(config: LambdaWorkerConfig) -> None:
            captured.append(config)

        _run_worker_internal(TEST_VERSION, configure, deps)
        wc = captured[0].worker_config
        assert wc.get("max_concurrent_activities") == DEFAULT_MAX_CONCURRENT_ACTIVITIES
        assert wc.get("disable_eager_activity_execution") is True
        dc = wc.get("deployment_config")
        assert dc is not None
        assert dc.use_worker_versioning is True
        assert dc.version == TEST_VERSION

    def test_no_deadline_runs_until_complete(self) -> None:
        """When no deadline is available, worker runs until it completes."""
        deps = _make_test_deps()
        handler = _run_worker_internal(TEST_VERSION, lambda config: None, deps)
        ctx = MagicMock(spec=["aws_request_id", "invoked_function_arn"])
        ctx.aws_request_id = "req-123"
        ctx.invoked_function_arn = "arn:aws:lambda:us-east-1:123:function:f"
        handler({}, ctx)
