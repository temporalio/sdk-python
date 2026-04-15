from collections.abc import AsyncGenerator, Callable
from datetime import timedelta

from google.adk.models import BaseLlm, LLMRegistry
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse

import temporalio.workflow
from temporalio import activity, workflow
from temporalio.workflow import ActivityConfig


class AdkActivityConfig(ActivityConfig, total=False):
    """Activity config with ADK-specific options.

    Attributes:
        summary_fn: Optional callable that receives the LlmRequest and returns
            a summary string (or None). Must be deterministic as it is called
            during workflow execution.
    """

    summary_fn: Callable[[LlmRequest], str | None] | None


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
        activity_config: AdkActivityConfig | ActivityConfig | None = None,
    ) -> None:
        """Initialize the TemporalModel.

        Args:
            model_name: The name of the model to use.
            activity_config: Configuration options for the activity execution.

        Raises:
            ValueError: If both 'summary' and 'summary_fn' are set.
        """
        super().__init__(model=model_name)
        self._model_name = model_name
        raw = dict(activity_config) if activity_config else {}
        self._summary_fn: Callable[[LlmRequest], str | None] | None = raw.pop(
            "summary_fn", None
        )  # type: ignore[assignment]
        raw.setdefault("start_to_close_timeout", timedelta(seconds=60))
        if raw.get("summary") is not None and self._summary_fn is not None:
            raise ValueError("Cannot specify both 'summary' and 'summary_fn'")
        self._activity_config = ActivityConfig(**raw)  # type: ignore[typeddict-item]

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
