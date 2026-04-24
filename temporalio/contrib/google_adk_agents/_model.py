import json
import logging
from collections.abc import AsyncGenerator, Callable
from datetime import datetime, timedelta, timezone

from google.adk.models import BaseLlm, LLMRegistry
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse

import temporalio.workflow
from temporalio import activity, workflow
from temporalio.contrib.pubsub import PubSubClient
from temporalio.workflow import ActivityConfig

logger = logging.getLogger(__name__)

EVENTS_TOPIC = "events"


def _make_event(event_type: str, **data: object) -> bytes:
    return json.dumps(
        {
            "type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
    ).encode()


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


@activity.defn
async def invoke_model_streaming(llm_request: LlmRequest) -> list[LlmResponse]:
    """Streaming-aware model activity.

    Calls the LLM with stream=True, publishes TEXT_DELTA events via
    PubSubClient as tokens arrive, and returns the collected responses.

    The PubSubClient auto-detects the activity context to find the parent
    workflow for publishing.

    Args:
        llm_request: The LLM request containing model name and parameters.

    Returns:
        List of LLM responses from the model.
    """
    if llm_request.model is None:
        raise ValueError("No model name provided, could not create LLM.")

    llm = LLMRegistry.new_llm(llm_request.model)
    if not llm:
        raise ValueError(f"Failed to create LLM for model: {llm_request.model}")

    pubsub = PubSubClient.from_activity(batch_interval=0.1)
    responses: list[LlmResponse] = []
    text_buffer = ""

    async with pubsub:
        pubsub.publish(EVENTS_TOPIC, _make_event("LLM_CALL_START"), force_flush=True)

        async for response in llm.generate_content_async(
            llm_request=llm_request, stream=True
        ):
            activity.heartbeat()
            responses.append(response)

            if response.content and response.content.parts:
                for part in response.content.parts:
                    if part.text:
                        text_buffer += part.text
                        pubsub.publish(
                            EVENTS_TOPIC,
                            _make_event("TEXT_DELTA", delta=part.text),
                        )
                    if part.function_call:
                        pubsub.publish(
                            EVENTS_TOPIC,
                            _make_event(
                                "TOOL_CALL_START",
                                tool_name=part.function_call.name,
                            ),
                        )

        if text_buffer:
            pubsub.publish(
                EVENTS_TOPIC,
                _make_event("TEXT_COMPLETE", text=text_buffer),
                force_flush=True,
            )
        pubsub.publish(EVENTS_TOPIC, _make_event("LLM_CALL_COMPLETE"), force_flush=True)

    return responses


class TemporalModel(BaseLlm):
    """A Temporal-based LLM model that executes model invocations as activities."""

    def __init__(
        self,
        model_name: str,
        activity_config: ActivityConfig | None = None,
        streaming: bool = False,
        *,
        summary_fn: Callable[[LlmRequest], str | None] | None = None,
    ) -> None:
        """Initialize the TemporalModel.

        Args:
            model_name: The name of the model to use.
            activity_config: Configuration options for the activity execution.
            streaming: When True, the model activity uses the streaming LLM
                endpoint and publishes token events via PubSubClient. The
                workflow is unaffected -- it still receives complete responses.
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
        self._streaming = streaming
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
            stream: Whether to stream the response (currently ignored; use the
                ``streaming`` constructor parameter instead).

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
        activity_fn = invoke_model_streaming if self._streaming else invoke_model
        responses = await workflow.execute_activity(
            activity_fn,
            args=[llm_request],
            **config,
        )
        for response in responses:
            yield response
