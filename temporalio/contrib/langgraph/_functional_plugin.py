"""LangGraph Functional API plugin for Temporal integration."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from temporalio.contrib.langgraph._functional_activity import execute_langgraph_task
from temporalio.contrib.langgraph._functional_registry import register_entrypoint
from temporalio.contrib.pydantic import PydanticPayloadConverter
from temporalio.converter import DataConverter, DefaultPayloadConverter
from temporalio.plugin import SimplePlugin
from temporalio.worker import WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from langgraph.pregel import Pregel


def _langgraph_data_converter(converter: DataConverter | None) -> DataConverter:
    """Configure data converter with PydanticPayloadConverter for LangChain messages."""
    if converter is None:
        return DataConverter(payload_converter_class=PydanticPayloadConverter)
    elif converter.payload_converter_class is DefaultPayloadConverter:
        return dataclasses.replace(
            converter, payload_converter_class=PydanticPayloadConverter
        )
    return converter


class LangGraphFunctionalPlugin(SimplePlugin):
    """Temporal plugin for LangGraph Functional API integration.

    .. warning::
        This class is experimental and may change in future versions.
        Use with caution in production environments.

    Registers @entrypoint functions, auto-registers the dynamic task activity,
    and configures the Pydantic data converter for LangChain messages.

    Example:
        ```python
        from langgraph.func import entrypoint, task

        @task
        async def my_task(x: int) -> int:
            return x * 2

        @entrypoint()
        async def my_entrypoint(x: int) -> int:
            return await my_task(x)

        plugin = LangGraphFunctionalPlugin(
            entrypoints={"my_entrypoint": my_entrypoint},
        )
        ```
    """

    def __init__(
        self,
        entrypoints: dict[str, Pregel],
        default_task_timeout: timedelta = timedelta(minutes=5),
        task_options: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Initialize the LangGraph Functional API plugin.

        Args:
            entrypoints: Mapping of entrypoint_id to @entrypoint Pregel object.
            default_task_timeout: Default timeout for task activities.
            task_options: Per-task activity options, keyed by task name.
        """
        self._entrypoints = entrypoints
        self._default_task_timeout = default_task_timeout
        self._task_options = task_options or {}

        logger.debug(
            "Initializing LangGraphFunctionalPlugin with %d entrypoints: %s",
            len(entrypoints),
            list(entrypoints.keys()),
        )

        # Register entrypoints in global registry
        for entrypoint_id, entrypoint in entrypoints.items():
            # Extract per-task options for this entrypoint
            per_task_options = self._task_options
            default_options = {"start_to_close_timeout": default_task_timeout}

            register_entrypoint(
                entrypoint_id,
                entrypoint,
                default_task_options=default_options,
                per_task_options=per_task_options,
            )
            logger.debug("Registered entrypoint: %s", entrypoint_id)

        def add_activities(
            activities: Sequence[Callable[..., Any]] | None,
        ) -> Sequence[Callable[..., Any]]:
            """Add the dynamic task execution activity."""
            return list(activities or []) + [execute_langgraph_task]

        def workflow_runner(runner: WorkflowRunner | None) -> WorkflowRunner:
            """Configure sandbox passthrough for LangGraph dependencies."""
            if not runner:
                raise ValueError(
                    "No WorkflowRunner provided to LangGraphFunctionalPlugin."
                )

            # Add passthrough modules for LangGraph functional API
            # langgraph is needed because @entrypoint runs in workflow sandbox
            if isinstance(runner, SandboxedWorkflowRunner):
                return dataclasses.replace(
                    runner,
                    restrictions=runner.restrictions.with_passthrough_modules(
                        "pydantic_core",
                        "langchain_core",
                        "annotated_types",
                        "langgraph",
                    ),
                )
            return runner

        super().__init__(
            name="LangGraphFunctionalPlugin",
            data_converter=_langgraph_data_converter,
            activities=add_activities,
            workflow_runner=workflow_runner,
        )

    def get_entrypoint_ids(self) -> list[str]:
        """Get list of registered entrypoint IDs."""
        return list(self._entrypoints.keys())

    def is_entrypoint_registered(self, entrypoint_id: str) -> bool:
        """Check if an entrypoint is registered."""
        return entrypoint_id in self._entrypoints
