"""Tests for temporalio.contrib.modal.worker."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from temporalio.common import WorkerDeploymentVersion
from temporalio.contrib.modal.worker._configure import (
    ModalWorkerConfig,
    _run_shutdown_hooks,
)
from temporalio.contrib.modal.worker._defaults import (
    DEFAULT_EXECUTION_TIMEOUT,
    DEFAULT_MAX_CACHED_WORKFLOWS,
    DEFAULT_MAX_CONCURRENT_ACTIVITIES,
    DEFAULT_MAX_CONCURRENT_LOCAL_ACTIVITIES,
    DEFAULT_MAX_CONCURRENT_NEXUS_TASKS,
    DEFAULT_MAX_CONCURRENT_WORKFLOW_TASKS,
    MIN_FUNCTION_TIMEOUT_HEADROOM,
    apply_modal_worker_defaults,
    build_modal_identity,
    modal_default_config_file_path,
)
from temporalio.contrib.modal.worker._run_worker import (
    _run_worker_internal,
    _run_worker_until_timeout,
    _WorkerDeps,
)
from temporalio.envconfig import ClientConfigProfile
from temporalio.worker import WorkerConfig

TEST_VERSION = WorkerDeploymentVersion(
    deployment_name="test-deployment",
    build_id="test-build",
)
TEST_MODAL_FUNCTION_TIMEOUT = DEFAULT_EXECUTION_TIMEOUT + MIN_FUNCTION_TIMEOUT_HEADROOM


def _make_test_deps(
    *,
    connect_kwargs_capture: list[dict[str, Any]] | None = None,
    worker_kwargs_capture: list[dict[str, Any]] | None = None,
) -> _WorkerDeps:
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

    env = {
        "TEMPORAL_TASK_QUEUE": "test-queue",
        "MODAL_TASK_ID": "ta-container",
    }
    return _WorkerDeps(
        connect=fake_connect,
        create_worker=fake_create_worker,
        load_config=lambda: ClientConfigProfile(),
        getenv=env.get,
        get_function_call_id=lambda: "fc-call",
        monotonic=lambda: 0,
    )


class TestModalWorkerConfig:
    def test_fields_are_modifiable(self) -> None:
        config = ModalWorkerConfig()
        config.client_connect_config["namespace"] = "custom-ns"
        config.worker_config["task_queue"] = "my-queue"
        config.execution_timeout = timedelta(minutes=10)
        config.shutdown_deadline_buffer = timedelta(seconds=10)

        assert config.client_connect_config["namespace"] == "custom-ns"
        assert config.worker_config["task_queue"] == "my-queue"
        assert config.execution_timeout == timedelta(minutes=10)
        assert config.shutdown_deadline_buffer == timedelta(seconds=10)

    def test_mutable_fields_are_independent(self) -> None:
        first = ModalWorkerConfig()
        second = ModalWorkerConfig()
        first.worker_config["max_concurrent_activities"] = 99
        first.shutdown_hooks.append(lambda: None)

        assert "max_concurrent_activities" not in second.worker_config
        assert not second.shutdown_hooks
        assert dataclasses.is_dataclass(ModalWorkerConfig)

    @pytest.mark.asyncio
    async def test_shutdown_hooks_run_in_order_and_continue_after_error(
        self,
    ) -> None:
        config = ModalWorkerConfig()
        calls: list[str] = []

        async def async_hook() -> None:
            calls.append("async")

        def failing_hook() -> None:
            calls.append("failing")
            raise RuntimeError("flush failed")

        config.shutdown_hooks.extend(
            [lambda: calls.append("sync"), failing_hook, async_hook]
        )
        await _run_shutdown_hooks(config)

        assert calls == ["sync", "failing", "async"]


class TestDefaults:
    def test_apply_modal_worker_defaults(self) -> None:
        config: WorkerConfig = {}
        apply_modal_worker_defaults(config)

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

    def test_defaults_preserve_overrides_but_always_disable_eager(self) -> None:
        config: WorkerConfig = {
            "max_concurrent_activities": 50,
            "disable_eager_activity_execution": False,
        }
        apply_modal_worker_defaults(config)

        assert config.get("max_concurrent_activities") == 50
        assert config.get("disable_eager_activity_execution") is True

    def test_build_modal_identity(self) -> None:
        assert build_modal_identity("fc-123", "ta-456") == "fc-123@ta-456"
        assert build_modal_identity("", "") == "unknown@unknown"

    def test_default_config_file_path(self) -> None:
        empty_env: dict[str, str] = {}
        configured_env = {"TEMPORAL_CONFIG_FILE": "/config/temporal.toml"}
        assert modal_default_config_file_path(empty_env.get) == Path("temporal.toml")
        assert modal_default_config_file_path(configured_env.get) == Path(
            "/config/temporal.toml"
        )


class TestRunWorkerUntilTimeout:
    @pytest.mark.asyncio
    async def test_budget_timeout_is_suppressed(self) -> None:
        worker = MagicMock()
        worker_cancelled = False

        async def run() -> None:
            nonlocal worker_cancelled
            try:
                await asyncio.Event().wait()
            finally:
                worker_cancelled = True

        worker.run = run

        await _run_worker_until_timeout(worker, 0.001)

        assert worker_cancelled

    @pytest.mark.asyncio
    async def test_worker_timeout_is_propagated(self) -> None:
        worker = MagicMock()
        worker_error = TimeoutError("worker failed")

        async def run() -> None:
            raise worker_error

        worker.run = run

        with pytest.raises(TimeoutError) as err:
            await _run_worker_until_timeout(worker, 1)

        assert err.value is worker_error


class TestRunWorkerInternal:
    @pytest.mark.asyncio
    async def test_success_applies_defaults_and_versioning(self) -> None:
        worker_capture: list[dict[str, Any]] = []
        deps = _make_test_deps(worker_kwargs_capture=worker_capture)

        await _run_worker_internal(
            TEST_VERSION,
            lambda config: None,
            TEST_MODAL_FUNCTION_TIMEOUT,
            deps,
        )

        worker_config = worker_capture[0]
        assert worker_config["task_queue"] == "test-queue"
        assert (
            worker_config["max_concurrent_activities"]
            == DEFAULT_MAX_CONCURRENT_ACTIVITIES
        )
        assert worker_config["disable_eager_activity_execution"] is True
        deployment_config = worker_config["deployment_config"]
        assert deployment_config.use_worker_versioning is True
        assert deployment_config.version == TEST_VERSION

    @pytest.mark.asyncio
    async def test_config_receives_defaults_and_can_override_them(self) -> None:
        connect_capture: list[dict[str, Any]] = []
        worker_capture: list[dict[str, Any]] = []
        deps = _make_test_deps(
            connect_kwargs_capture=connect_capture,
            worker_kwargs_capture=worker_capture,
        )

        def configure(config: ModalWorkerConfig) -> None:
            assert config.execution_timeout == DEFAULT_EXECUTION_TIMEOUT
            assert (
                config.worker_config.get("max_concurrent_activities")
                == DEFAULT_MAX_CONCURRENT_ACTIVITIES
            )
            config.client_connect_config["namespace"] = "custom-ns"
            config.worker_config["task_queue"] = "custom-queue"
            config.worker_config["max_concurrent_activities"] = 42

        await _run_worker_internal(
            TEST_VERSION, configure, TEST_MODAL_FUNCTION_TIMEOUT, deps
        )

        assert connect_capture[0]["namespace"] == "custom-ns"
        assert worker_capture[0]["task_queue"] == "custom-queue"
        assert worker_capture[0]["max_concurrent_activities"] == 42

    @pytest.mark.asyncio
    async def test_identity_uses_function_call_and_container(self) -> None:
        connect_capture: list[dict[str, Any]] = []
        deps = _make_test_deps(connect_kwargs_capture=connect_capture)

        await _run_worker_internal(
            TEST_VERSION,
            lambda config: None,
            TEST_MODAL_FUNCTION_TIMEOUT,
            deps,
        )

        assert connect_capture[0]["identity"] == "fc-call@ta-container"

    @pytest.mark.asyncio
    async def test_user_identity_override_does_not_read_modal_context(self) -> None:
        connect_capture: list[dict[str, Any]] = []
        deps = _make_test_deps(connect_kwargs_capture=connect_capture)
        get_function_call_id = MagicMock(side_effect=AssertionError("not expected"))
        deps.get_function_call_id = get_function_call_id

        def configure(config: ModalWorkerConfig) -> None:
            config.client_connect_config["identity"] = "custom-worker"

        await _run_worker_internal(
            TEST_VERSION, configure, TEST_MODAL_FUNCTION_TIMEOUT, deps
        )

        assert connect_capture[0]["identity"] == "custom-worker"
        get_function_call_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_task_queue(self) -> None:
        deps = _make_test_deps()
        deps.getenv = lambda _name: None

        with pytest.raises(ValueError, match="task queue not configured"):
            await _run_worker_internal(
                TEST_VERSION,
                lambda config: None,
                TEST_MODAL_FUNCTION_TIMEOUT,
                deps,
            )

    @pytest.mark.asyncio
    async def test_missing_version(self) -> None:
        deps = _make_test_deps()
        with pytest.raises(ValueError, match="version is required"):
            await _run_worker_internal(
                WorkerDeploymentVersion(deployment_name="", build_id=""),
                lambda config: None,
                TEST_MODAL_FUNCTION_TIMEOUT,
                deps,
            )

    @pytest.mark.asyncio
    async def test_function_timeout_requires_headroom(self) -> None:
        connect_capture: list[dict[str, Any]] = []
        worker_capture: list[dict[str, Any]] = []
        deps = _make_test_deps(
            connect_kwargs_capture=connect_capture,
            worker_kwargs_capture=worker_capture,
        )

        with pytest.raises(
            ValueError,
            match=("modal_function_timeout must be at least execution_timeout \\+ 15s"),
        ):
            await _run_worker_internal(
                TEST_VERSION,
                lambda config: None,
                DEFAULT_EXECUTION_TIMEOUT,
                deps,
            )

        assert not connect_capture
        assert not worker_capture

    @pytest.mark.asyncio
    async def test_function_timeout_uses_configured_execution_timeout(self) -> None:
        deps = _make_test_deps()

        def configure(config: ModalWorkerConfig) -> None:
            config.execution_timeout = DEFAULT_EXECUTION_TIMEOUT + timedelta(seconds=1)

        with pytest.raises(ValueError, match="316.0s required, 315.0s configured"):
            await _run_worker_internal(
                TEST_VERSION,
                configure,
                TEST_MODAL_FUNCTION_TIMEOUT,
                deps,
            )

    @pytest.mark.asyncio
    async def test_timeout_too_short_after_setup(self) -> None:
        deps = _make_test_deps()
        shutdown_called = False

        def configure(config: ModalWorkerConfig) -> None:
            config.execution_timeout = timedelta(seconds=8)
            config.shutdown_deadline_buffer = timedelta(seconds=7)
            config.shutdown_hooks.append(_mark_shutdown)

        def _mark_shutdown() -> None:
            nonlocal shutdown_called
            shutdown_called = True

        with pytest.raises(RuntimeError, match="execution timeout is too short"):
            await _run_worker_internal(
                TEST_VERSION, configure, TEST_MODAL_FUNCTION_TIMEOUT, deps
            )
        assert shutdown_called

    @pytest.mark.asyncio
    async def test_short_work_time_logs_warning(self) -> None:
        deps = _make_test_deps()

        def configure(config: ModalWorkerConfig) -> None:
            config.execution_timeout = timedelta(seconds=10)
            config.shutdown_deadline_buffer = timedelta(seconds=7)

        with patch("temporalio.contrib.modal.worker._run_worker.logger") as mock_logger:
            await _run_worker_internal(
                TEST_VERSION, configure, TEST_MODAL_FUNCTION_TIMEOUT, deps
            )

        mock_logger.warning.assert_called_once()
        assert "less than 5s" in mock_logger.warning.call_args[0][0]

    @pytest.mark.asyncio
    async def test_budget_includes_configuration_and_connection(self) -> None:
        deps = _make_test_deps()
        times = iter([100.0, 102.0, 105.0])
        deps.monotonic = lambda: next(times)
        captured_timeout: list[float] = []

        async def fake_wait_for(awaitable: Awaitable[None], timeout: float) -> None:
            captured_timeout.append(timeout)
            await awaitable

        def configure(config: ModalWorkerConfig) -> None:
            config.execution_timeout = timedelta(seconds=20)
            config.shutdown_deadline_buffer = timedelta(seconds=7)

        with patch(
            "temporalio.contrib.modal.worker._run_worker.asyncio.wait_for",
            fake_wait_for,
        ):
            await _run_worker_internal(
                TEST_VERSION, configure, TEST_MODAL_FUNCTION_TIMEOUT, deps
            )

        assert captured_timeout == [8.0]

    @pytest.mark.asyncio
    async def test_shutdown_hooks_run_on_worker_error(self) -> None:
        deps = _make_test_deps()
        shutdown_called = False

        async def failing_run() -> None:
            raise RuntimeError("worker failed")

        def create_worker(_client: Any, **_kwargs: Any) -> Any:
            worker = MagicMock()
            worker.run = failing_run
            return worker

        deps.create_worker = create_worker

        def configure(config: ModalWorkerConfig) -> None:
            def shutdown() -> None:
                nonlocal shutdown_called
                shutdown_called = True

            config.shutdown_hooks.append(shutdown)

        with pytest.raises(RuntimeError, match="worker failed"):
            await _run_worker_internal(
                TEST_VERSION, configure, TEST_MODAL_FUNCTION_TIMEOUT, deps
            )
        assert shutdown_called

    @pytest.mark.asyncio
    async def test_modal_cancellation_propagates_after_shutdown_hooks(self) -> None:
        deps = _make_test_deps()
        shutdown_called = False

        async def cancelled_run() -> None:
            raise asyncio.CancelledError

        def create_worker(_client: Any, **_kwargs: Any) -> Any:
            worker = MagicMock()
            worker.run = cancelled_run
            return worker

        deps.create_worker = create_worker

        def configure(config: ModalWorkerConfig) -> None:
            def shutdown() -> None:
                nonlocal shutdown_called
                shutdown_called = True

            config.shutdown_hooks.append(shutdown)

        with pytest.raises(asyncio.CancelledError):
            await _run_worker_internal(
                TEST_VERSION, configure, TEST_MODAL_FUNCTION_TIMEOUT, deps
            )
        assert shutdown_called

    @pytest.mark.asyncio
    async def test_async_generator_brackets_worker_and_hooks(self) -> None:
        events: list[str] = []
        deps = _make_test_deps()

        original_connect = deps.connect

        async def connect(**kwargs: Any) -> Any:
            events.append("connect")
            return await original_connect(**kwargs)

        deps.connect = connect

        async def configure(config: ModalWorkerConfig):
            events.append("setup")
            config.shutdown_hooks.append(lambda: events.append("shutdown"))
            yield
            events.append("teardown")

        await _run_worker_internal(
            TEST_VERSION, configure, TEST_MODAL_FUNCTION_TIMEOUT, deps
        )

        assert events == ["setup", "connect", "shutdown", "teardown"]

    @pytest.mark.asyncio
    async def test_async_context_manager_configure(self) -> None:
        events: list[str] = []
        deps = _make_test_deps()

        @asynccontextmanager
        async def configure(_config: ModalWorkerConfig):
            events.append("setup")
            yield
            events.append("teardown")

        await _run_worker_internal(
            TEST_VERSION, configure, TEST_MODAL_FUNCTION_TIMEOUT, deps
        )

        assert events == ["setup", "teardown"]
