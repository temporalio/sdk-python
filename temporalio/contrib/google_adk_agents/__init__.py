# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Temporal Integration for ADK.

This module provides the necessary components to run ADK Agents within Temporal Workflows.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import inspect
import time
import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any, AsyncGenerator, Callable, List, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.models import BaseLlm, LLMRegistry, LlmRequest, LlmResponse
from google.adk.plugins import BasePlugin
from google.genai import types

from temporalio import activity, workflow
from temporalio.common import RawValue, RetryPolicy
from temporalio.contrib.pydantic import (
    PydanticPayloadConverter as _DefaultPydanticPayloadConverter,
)
from temporalio.converter import DataConverter, DefaultPayloadConverter
from temporalio.plugin import SimplePlugin
from temporalio.worker import (
    ExecuteWorkflowInput,
    Interceptor,
    UnsandboxedWorkflowRunner,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowRunner,
)


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


class AdkWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        # Global runtime setup before ANY user code runs
        setup_deterministic_runtime()
        return await super().execute_workflow(input)


class AdkInterceptor(Interceptor):
    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor] | None:
        return AdkWorkflowInboundInterceptor


class AgentPlugin(BasePlugin):
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
        wrapper.__signature__ = inspect.signature(activity_def)

        return wrapper

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        # Construct dynamic activity name for visibility
        agent_name = callback_context.agent_name
        activity_name = f"{agent_name}.generate_content"

        # Execute with dynamic name
        response_dicts = await workflow.execute_activity(
            activity_name, args=[llm_request], **self.activity_options
        )

        # Rehydrate LlmResponse objects safely
        responses = []
        for d in response_dicts:
            try:
                responses.append(LlmResponse.model_validate(d))
            except Exception as e:
                raise RuntimeError(
                    f"Failed to deserialized LlmResponse from activity result: {e}"
                ) from e

        # Simple consolidation: return the last complete response
        return responses[-1] if responses else None


class WorkerPlugin(SimplePlugin):
    """A Temporal Worker Plugin configured for ADK.

    This plugin configures:
    1. Pydantic Payload Converter (required for ADK objects).
    2. Sandbox Passthrough for `google.adk` and `google.genai`.
    """

    def __init__(self):
        super().__init__(
            name="adk_worker_plugin",
            data_converter=self._configure_data_converter,
            workflow_runner=self._configure_workflow_runner,
            activities=[self.dynamic_activity],
            worker_interceptors=[AdkInterceptor()],
        )

    @staticmethod
    @activity.defn(dynamic=True)
    async def dynamic_activity(args: Sequence[RawValue]) -> Any:
        """Handles dynamic ADK activities (e.g. 'AgentName.generate_content')."""
        activity_type = activity.info().activity_type

        # Check if this is a generate_content call
        if (
            activity_type.endswith(".generate_content")
            or activity_type == "google.adk.generate_content"
        ):
            return await WorkerPlugin._handle_generate_content(args)

        raise ValueError(f"Unknown dynamic activity: {activity_type}")

    @staticmethod
    async def _handle_generate_content(args: List[Any]) -> list[dict[str, Any]]:
        """Implementation of content generation."""
        # 1. Decode Arguments
        # Dynamic activities receive RawValue wrappers (which host the Payload).
        # We must manually decode them using the activity's configured data converter.
        converter = activity.payload_converter()

        # We expect a single argument: LlmRequest
        if not args:
            raise ValueError("Missing llm_request argument for generate_content")

        # Extract payloads from RawValue wrappers
        payloads = [arg.payload for arg in args]

        # Decode
        # from_payloads returns a list of decoded objects.
        # We specify the types we expect for each argument.
        try:
            decoded_args = converter.from_payloads(payloads, [LlmRequest])
            llm_request: LlmRequest = decoded_args[0]
        except Exception as e:
            activity.logger.error(f"Failed to decode arguments: {e}")
            raise ValueError(f"Argument decoding failed: {e}") from e

        # 3. Model Initialization
        llm = LLMRegistry.new_llm(llm_request.model)
        if not llm:
            raise ValueError(f"Failed to create LLM for model: {llm_request.model}")

        # 4. Execution
        responses = [
            response
            async for response in llm.generate_content_async(llm_request=llm_request)
        ]

        # 5. Serialization
        # Return dicts to avoid Pydantic strictness issues on rehydration
        return [r.model_dump(mode="json", by_alias=True) for r in responses]

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

    def _configure_workflow_runner(
        self, runner: WorkflowRunner | None
    ) -> WorkflowRunner:
        from temporalio.worker import UnsandboxedWorkflowRunner

        # TODO: Not sure implications here. is this a good default an allow user override?
        return UnsandboxedWorkflowRunner()
