from __future__ import annotations

import dataclasses
import inspect
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LLMRegistry
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins import BasePlugin

from temporalio import activity, workflow
from temporalio.contrib.google_adk_agents._mcp import TemporalToolSetProvider
from temporalio.contrib.pydantic import (
    PydanticPayloadConverter as _DefaultPydanticPayloadConverter,
)
from temporalio.converter import DataConverter, DefaultPayloadConverter
from temporalio.plugin import SimplePlugin
from temporalio.worker import (
    WorkflowRunner,
)
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner


def setup_deterministic_runtime():
    """Configures ADK runtime for Temporal determinism.

    This should be called at the start of a Temporal Workflow before any ADK components
    (like SessionService) are used, if they rely on runtime.get_time() or runtime.new_uuid().
    """
    try:
        from google.adk import runtime

        # Define safer, context-aware providers
        def _deterministic_time_provider() -> float:
            if workflow.in_workflow():
                return workflow.now().timestamp()
            return time.time()

        def _deterministic_id_provider() -> str:
            if workflow.in_workflow():
                return str(workflow.uuid4())
            return str(uuid.uuid4())

        runtime.set_time_provider(_deterministic_time_provider)
        runtime.set_id_provider(_deterministic_id_provider)
    except ImportError:
        pass
    except Exception as e:
        print(f"Warning: Failed to set deterministic runtime providers: {e}")


class AdkAgentPlugin(BasePlugin):
    """ADK Plugin for Temporal integration.

    This plugin automatically configures the ADK runtime to be deterministic when running
    inside a Temporal workflow, and intercepts model calls to execute them as Temporal Activities.
    """

    def __init__(self, activity_options: Optional[dict[str, Any]] = None):
        """Initializes the Temporal Plugin.

        Args:
            activity_options: Default options for model activities (e.g. start_to_close_timeout).
        """
        super().__init__(name="temporal_plugin")
        self.activity_options = activity_options or {}

    @staticmethod
    def activity_tool(activity_def: Callable, **kwargs: Any) -> Callable:
        """Decorator/Wrapper to wrap a Temporal Activity as an ADK Tool.

        This ensures the activity's signature is preserved for ADK's tool schema generation
        while marking it as a tool that executes via 'workflow.execute_activity'.
        """

        async def wrapper(*args, **kw):
            # Inspect signature to bind arguments
            sig = inspect.signature(activity_def)
            bound = sig.bind(*args, **kw)
            bound.apply_defaults()

            # Convert to positional args for Temporal
            activity_args = list(bound.arguments.values())

            # Decorator kwargs are defaults.
            options = kwargs.copy()

            return await workflow.execute_activity(
                activity_def, *activity_args, **options
            )

        # Copy metadata
        wrapper.__name__ = activity_def.__name__
        wrapper.__doc__ = activity_def.__doc__
        setattr(wrapper, "__signature__", inspect.signature(activity_def))

        return wrapper

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        responses = await workflow.execute_activity(
            invoke_model,
            args=[llm_request],
            summary=callback_context.agent_name,
            **self.activity_options,
        )

        # Simple consolidation: return the last complete response
        return responses[-1] if responses else None


@activity.defn
async def invoke_model(llm_request: LlmRequest) -> list[LlmResponse]:
    if llm_request.model is None:
        raise ValueError(f"No model name provided, could not create LLM.")

    llm = LLMRegistry.new_llm(llm_request.model)
    if not llm:
        raise ValueError(f"Failed to create LLM for model: {llm_request.model}")

    return [
        response
        async for response in llm.generate_content_async(llm_request=llm_request)
    ]


class TemporalAdkPlugin(SimplePlugin):
    """A Temporal Worker Plugin configured for ADK.

    This plugin configures:
    1. Pydantic Payload Converter (required for ADK objects).
    2. Sandbox Passthrough for `google.adk` and `google.genai`.
    """

    def __init__(self, toolset_providers: list[TemporalToolSetProvider] = ()):
        @asynccontextmanager
        async def run_context() -> AsyncIterator[None]:
            setup_deterministic_runtime()
            yield

        def workflow_runner(runner: WorkflowRunner | None) -> WorkflowRunner:
            if not runner:
                raise ValueError("No WorkflowRunner provided to the ADK plugin.")

            # If in sandbox, add additional passthrough
            if isinstance(runner, SandboxedWorkflowRunner):
                return dataclasses.replace(
                    runner,
                    restrictions=runner.restrictions.with_passthrough_modules(
                        "google.adk", "google.genai", "mcp"
                    ),
                )
            return runner

        new_activities = [invoke_model]
        for toolset_provider in toolset_providers:
            new_activities.extend(toolset_provider._get_activities())

        super().__init__(
            name="google_adk_plugin",
            data_converter=self._configure_data_converter,
            activities=new_activities,
            run_context=lambda: run_context(),
            workflow_runner=workflow_runner,
        )

    def _configure_data_converter(
        self, converter: DataConverter | None
    ) -> DataConverter:
        if converter is None:
            return DataConverter(
                payload_converter_class=_DefaultPydanticPayloadConverter
            )
        elif converter.payload_converter_class is DefaultPayloadConverter:
            return dataclasses.replace(
                converter, payload_converter_class=_DefaultPydanticPayloadConverter
            )
        return converter
