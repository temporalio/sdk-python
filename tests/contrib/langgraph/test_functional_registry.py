"""Unit tests for LangGraph Functional API registry.

Tests for EntrypointRegistry, global registry functions, and error classes.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class TestEntrypointNotFoundError:
    """Tests for EntrypointNotFoundError."""

    def test_error_message_format(self) -> None:
        """Error message should include entrypoint ID and available list."""
        from temporalio.contrib.langgraph._functional_registry import (
            EntrypointNotFoundError,
        )

        error = EntrypointNotFoundError("missing_ep", ["ep1", "ep2"])

        assert "missing_ep" in str(error)
        assert "ep1" in str(error)
        assert "ep2" in str(error)
        assert error.type == "ENTRYPOINT_NOT_FOUND"
        assert error.non_retryable is True

    def test_error_with_empty_available_list(self) -> None:
        """Error should work with empty available list."""
        from temporalio.contrib.langgraph._functional_registry import (
            EntrypointNotFoundError,
        )

        error = EntrypointNotFoundError("missing_ep", [])

        assert "missing_ep" in str(error)
        assert "[]" in str(error)


class TestEntrypointAlreadyRegisteredError:
    """Tests for EntrypointAlreadyRegisteredError."""

    def test_error_message_format(self) -> None:
        """Error message should include entrypoint ID."""
        from temporalio.contrib.langgraph._functional_registry import (
            EntrypointAlreadyRegisteredError,
        )

        error = EntrypointAlreadyRegisteredError("duplicate_ep")

        assert "duplicate_ep" in str(error)
        assert "already registered" in str(error)
        assert error.type == "ENTRYPOINT_ALREADY_REGISTERED"
        assert error.non_retryable is True


class TestEntrypointRegistry:
    """Tests for EntrypointRegistry class."""

    def test_register_and_get(self) -> None:
        """Registry should store and retrieve entrypoints."""
        from temporalio.contrib.langgraph._functional_registry import (
            EntrypointRegistry,
        )

        registry = EntrypointRegistry()
        mock_pregel = MagicMock()

        registry.register("my_ep", mock_pregel)
        result = registry.get("my_ep")

        assert result is mock_pregel

    def test_register_duplicate_raises_error(self) -> None:
        """Registering same ID twice should raise error."""
        from temporalio.contrib.langgraph._functional_registry import (
            EntrypointAlreadyRegisteredError,
            EntrypointRegistry,
        )

        registry = EntrypointRegistry()
        mock_pregel = MagicMock()

        registry.register("my_ep", mock_pregel)

        with pytest.raises(EntrypointAlreadyRegisteredError) as exc_info:
            registry.register("my_ep", mock_pregel)

        assert "my_ep" in str(exc_info.value)

    def test_get_nonexistent_raises_error(self) -> None:
        """Getting nonexistent entrypoint should raise error."""
        from temporalio.contrib.langgraph._functional_registry import (
            EntrypointNotFoundError,
            EntrypointRegistry,
        )

        registry = EntrypointRegistry()
        registry.register("existing_ep", MagicMock())

        with pytest.raises(EntrypointNotFoundError) as exc_info:
            registry.get("nonexistent_ep")

        assert "nonexistent_ep" in str(exc_info.value)
        assert "existing_ep" in str(exc_info.value)

    def test_register_with_default_task_options(self) -> None:
        """Registry should store default task options."""
        from temporalio.contrib.langgraph._functional_registry import (
            EntrypointRegistry,
        )

        registry = EntrypointRegistry()
        default_options = {"start_to_close_timeout_seconds": 300.0}

        registry.register("my_ep", MagicMock(), default_task_options=default_options)

        assert registry.get_default_task_options("my_ep") == default_options

    def test_register_with_per_task_options(self) -> None:
        """Registry should store per-task options."""
        from temporalio.contrib.langgraph._functional_registry import (
            EntrypointRegistry,
        )

        registry = EntrypointRegistry()
        per_task_options = {
            "slow_task": {"start_to_close_timeout_seconds": 900.0},
            "fast_task": {"start_to_close_timeout_seconds": 30.0},
        }

        registry.register("my_ep", MagicMock(), per_task_options=per_task_options)

        assert registry.get_per_task_options("my_ep") == per_task_options

    def test_get_options_for_unregistered_entrypoint(self) -> None:
        """Getting options for unregistered entrypoint should return empty dict."""
        from temporalio.contrib.langgraph._functional_registry import (
            EntrypointRegistry,
        )

        registry = EntrypointRegistry()

        assert registry.get_default_task_options("unknown") == {}
        assert registry.get_per_task_options("unknown") == {}

    def test_list_entrypoints(self) -> None:
        """list_entrypoints should return all registered IDs."""
        from temporalio.contrib.langgraph._functional_registry import (
            EntrypointRegistry,
        )

        registry = EntrypointRegistry()
        registry.register("ep1", MagicMock())
        registry.register("ep2", MagicMock())
        registry.register("ep3", MagicMock())

        entrypoints = registry.list_entrypoints()

        assert sorted(entrypoints) == ["ep1", "ep2", "ep3"]

    def test_list_entrypoints_empty_registry(self) -> None:
        """list_entrypoints should return empty list for empty registry."""
        from temporalio.contrib.langgraph._functional_registry import (
            EntrypointRegistry,
        )

        registry = EntrypointRegistry()

        assert registry.list_entrypoints() == []

    def test_is_registered(self) -> None:
        """is_registered should correctly identify registered entrypoints."""
        from temporalio.contrib.langgraph._functional_registry import (
            EntrypointRegistry,
        )

        registry = EntrypointRegistry()
        registry.register("my_ep", MagicMock())

        assert registry.is_registered("my_ep") is True
        assert registry.is_registered("unknown_ep") is False

    def test_clear(self) -> None:
        """clear should remove all registered entrypoints."""
        from temporalio.contrib.langgraph._functional_registry import (
            EntrypointRegistry,
        )

        registry = EntrypointRegistry()
        registry.register(
            "ep1",
            MagicMock(),
            default_task_options={"timeout": 100},
            per_task_options={"task1": {"timeout": 50}},
        )
        registry.register("ep2", MagicMock())

        registry.clear()

        assert registry.list_entrypoints() == []
        assert registry.get_default_task_options("ep1") == {}
        assert registry.get_per_task_options("ep1") == {}


class TestGlobalRegistry:
    """Tests for global registry functions."""

    def setup_method(self) -> None:
        """Clear global registry before each test."""
        from temporalio.contrib.langgraph._functional_registry import (
            get_global_entrypoint_registry,
        )

        get_global_entrypoint_registry().clear()

    def teardown_method(self) -> None:
        """Clear global registry after each test."""
        from temporalio.contrib.langgraph._functional_registry import (
            get_global_entrypoint_registry,
        )

        get_global_entrypoint_registry().clear()

    def test_register_entrypoint(self) -> None:
        """register_entrypoint should add to global registry."""
        from temporalio.contrib.langgraph._functional_registry import (
            get_global_entrypoint_registry,
            register_entrypoint,
        )

        mock_pregel = MagicMock()
        register_entrypoint("test_ep", mock_pregel)

        registry = get_global_entrypoint_registry()
        assert registry.is_registered("test_ep")

    def test_get_entrypoint(self) -> None:
        """get_entrypoint should retrieve from global registry."""
        from temporalio.contrib.langgraph._functional_registry import (
            get_entrypoint,
            register_entrypoint,
        )

        mock_pregel = MagicMock()
        register_entrypoint("test_ep", mock_pregel)

        result = get_entrypoint("test_ep")

        assert result is mock_pregel

    def test_get_entrypoint_task_options(self) -> None:
        """get_entrypoint_task_options should retrieve per-task options."""
        from temporalio.contrib.langgraph._functional_registry import (
            get_entrypoint_task_options,
            register_entrypoint,
        )

        mock_pregel = MagicMock()
        per_task_options = {"task1": {"timeout": 100}}
        register_entrypoint("test_ep", mock_pregel, per_task_options=per_task_options)

        result = get_entrypoint_task_options("test_ep")

        assert result == per_task_options

    def test_get_entrypoint_default_options(self) -> None:
        """get_entrypoint_default_options should retrieve default options."""
        from temporalio.contrib.langgraph._functional_registry import (
            get_entrypoint_default_options,
            register_entrypoint,
        )

        mock_pregel = MagicMock()
        default_options = {"start_to_close_timeout_seconds": 600.0}
        register_entrypoint("test_ep", mock_pregel, default_task_options=default_options)

        result = get_entrypoint_default_options("test_ep")

        assert result == default_options

    def test_get_global_entrypoint_registry_returns_same_instance(self) -> None:
        """get_global_entrypoint_registry should return singleton."""
        from temporalio.contrib.langgraph._functional_registry import (
            get_global_entrypoint_registry,
        )

        registry1 = get_global_entrypoint_registry()
        registry2 = get_global_entrypoint_registry()

        assert registry1 is registry2
