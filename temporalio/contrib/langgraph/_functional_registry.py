"""Thread-safe registry for LangGraph functional API entrypoints.

This module provides registration and lookup for @entrypoint-decorated functions
that will be executed as Temporal workflows.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from temporalio.exceptions import ApplicationError

if TYPE_CHECKING:
    from langgraph.pregel import Pregel


class EntrypointNotFoundError(ApplicationError):
    """Raised when an entrypoint is not found in the registry."""

    def __init__(self, entrypoint_id: str, available: list[str]) -> None:
        """Initialize with the missing entrypoint ID and available entrypoints."""
        super().__init__(
            f"Entrypoint '{entrypoint_id}' not found. "
            f"Available entrypoints: {available}",
            type="ENTRYPOINT_NOT_FOUND",
            non_retryable=True,
        )


class EntrypointAlreadyRegisteredError(ApplicationError):
    """Raised when trying to register an entrypoint that already exists."""

    def __init__(self, entrypoint_id: str) -> None:
        """Initialize with the duplicate entrypoint ID."""
        super().__init__(
            f"Entrypoint '{entrypoint_id}' is already registered.",
            type="ENTRYPOINT_ALREADY_REGISTERED",
            non_retryable=True,
        )


class EntrypointRegistry:
    """Thread-safe registry for LangGraph functional API entrypoints.

    Stores @entrypoint-decorated functions (which are Pregel objects) for
    lookup during workflow execution.
    """

    def __init__(self) -> None:
        """Initialize empty registry."""
        self._entrypoints: dict[str, Pregel] = {}
        self._task_options: dict[str, dict[str, dict[str, Any]]] = {}
        self._default_task_options: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def register(
        self,
        entrypoint_id: str,
        entrypoint: Pregel,
        default_task_options: dict[str, Any] | None = None,
        per_task_options: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Register an entrypoint by ID.

        Args:
            entrypoint_id: Unique identifier for the entrypoint.
            entrypoint: The Pregel object returned by @entrypoint decorator.
            default_task_options: Default activity options for all tasks.
            per_task_options: Per-task activity options, keyed by task name.
        """
        with self._lock:
            if entrypoint_id in self._entrypoints:
                raise EntrypointAlreadyRegisteredError(entrypoint_id)

            self._entrypoints[entrypoint_id] = entrypoint

            if default_task_options:
                self._default_task_options[entrypoint_id] = default_task_options
            if per_task_options:
                self._task_options[entrypoint_id] = per_task_options

    def get(self, entrypoint_id: str) -> Pregel:
        """Get an entrypoint by ID.

        Args:
            entrypoint_id: The entrypoint identifier.

        Returns:
            The Pregel object for the entrypoint.

        Raises:
            EntrypointNotFoundError: If the entrypoint is not registered.
        """
        with self._lock:
            if entrypoint_id not in self._entrypoints:
                available = list(self._entrypoints.keys())
                raise EntrypointNotFoundError(entrypoint_id, available)
            return self._entrypoints[entrypoint_id]

    def get_default_task_options(self, entrypoint_id: str) -> dict[str, Any]:
        """Get default task options for an entrypoint."""
        return self._default_task_options.get(entrypoint_id, {})

    def get_per_task_options(self, entrypoint_id: str) -> dict[str, dict[str, Any]]:
        """Get per-task options for an entrypoint."""
        return self._task_options.get(entrypoint_id, {})

    def list_entrypoints(self) -> list[str]:
        """List all registered entrypoint IDs."""
        with self._lock:
            return list(self._entrypoints.keys())

    def is_registered(self, entrypoint_id: str) -> bool:
        """Check if an entrypoint is registered."""
        with self._lock:
            return entrypoint_id in self._entrypoints

    def clear(self) -> None:
        """Clear all registered entrypoints. Mainly for testing."""
        with self._lock:
            self._entrypoints.clear()
            self._task_options.clear()
            self._default_task_options.clear()


# Global registry instance
_global_registry = EntrypointRegistry()


def get_global_entrypoint_registry() -> EntrypointRegistry:
    """Get the global entrypoint registry instance."""
    return _global_registry


def register_entrypoint(
    entrypoint_id: str,
    entrypoint: Pregel,
    default_task_options: dict[str, Any] | None = None,
    per_task_options: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Register an entrypoint in the global registry."""
    _global_registry.register(
        entrypoint_id, entrypoint, default_task_options, per_task_options
    )


def get_entrypoint(entrypoint_id: str) -> Pregel:
    """Get an entrypoint from the global registry."""
    return _global_registry.get(entrypoint_id)


def get_entrypoint_task_options(entrypoint_id: str) -> dict[str, dict[str, Any]]:
    """Get per-task options for an entrypoint from the global registry."""
    return _global_registry.get_per_task_options(entrypoint_id)


def get_entrypoint_default_options(entrypoint_id: str) -> dict[str, Any]:
    """Get default task options for an entrypoint from the global registry."""
    return _global_registry.get_default_task_options(entrypoint_id)
