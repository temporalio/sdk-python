from collections.abc import AsyncGenerator, Callable
from datetime import timedelta

from google.adk.models import BaseLlm, LLMRegistry
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse

import temporalio.workflow
from temporalio import activity, workflow
from temporalio.workflow import ActivityConfig


@activity.defn
async def invoke_model(llm_request: LlmRequest) -> list[LlmResponse]:
    """Activity that invokes an LLM model.

    Args:
        llm_request: The LLM request containing model name and parameters.

    Returns:
        List of LLM responses from the model.

    Raises:
        ValueError: If model name is not provided or LLM creation fails.
    """
    if llm_request.model is None:
        raise ValueError(f"No model name provided, could not create LLM.")

    llm = LLMRegistry.new_llm(llm_request.model)
    if not llm:
        raise ValueError(f"Failed to create LLM for model: {llm_request.model}")

    return [
        response
        async for response in llm.generate_content_async(llm_request=llm_request)
    ]


class TemporalModel(BaseLlm):
    """A Temporal-based LLM model that executes model invocations as activities."""

    def __init__(
        self,
        model_name: str,
        activity_config: ActivityConfig | None = None,
        *,
        summary_fn: Callable[[LlmRequest], str | None] | None = None,
    ) -> None:
        """Initialize the TemporalModel.

        Args:
            model_name: The name of the model to use.
            activity_config: Configuration options for the activity execution.
            summary_fn: Optional callable that receives the LlmRequest and
                returns a summary string (or None) for the activity. Must be
                deterministic as it is called during workflow execution. If
                the callable raises, the exception will propagate and fail
                the workflow task.

        Raises:
            ValueError: If both ``ActivityConfig["summary"]`` and ``summary_fn`` are set.
        """
        super().__init__(model=model_name)
        self._model_name = model_name
        self._summary_fn = summary_fn
        self._activity_config = ActivityConfig(
            start_to_close_timeout=timedelta(seconds=60)
        )
        if activity_config is not None:
            if summary_fn is not None and activity_config.get("summary") is not None:
                raise ValueError(
                    "Cannot specify both ActivityConfig 'summary' and 'summary_fn'"
                )
            self._activity_config.update(activity_config)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        """Generate content asynchronously by executing model invocation as a Temporal activity.

        Args:
            llm_request: The LLM request containing model parameters and content.
            stream: Whether to stream the response (currently ignored).

        Yields:
            The responses from the model.
        """
        # If executed outside a workflow, like when doing local adk runs, use the model directly
        if not temporalio.workflow.in_workflow():
            async for response in LLMRegistry.new_llm(
                self._model_name
            ).generate_content_async(llm_request, stream=stream):
                yield response
            return

        config = self._activity_config.copy()
        if self._summary_fn is not None:
            summary = self._summary_fn(llm_request)
            if summary is not None:
                config["summary"] = summary
        elif "summary" not in config:
            if llm_request.config and llm_request.config.labels:
                agent_name = llm_request.config.labels.get("adk_agent_name")
                if agent_name:
                    config["summary"] = agent_name
        responses = await workflow.execute_activity(
            invoke_model,
            args=[llm_request],
            **config,
        )
        for response in responses:
            yield response
