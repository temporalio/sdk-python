from collections.abc import AsyncGenerator, Callable
from datetime import timedelta

from google.adk.models import BaseLlm, LLMRegistry
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse

import temporalio.workflow
from temporalio import activity, workflow
from temporalio.contrib.pubsub import PubSubClient
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


@activity.defn
async def invoke_model_streaming(
    llm_request: LlmRequest,
    streaming_event_topic: str | None,
    streaming_event_batch_interval: timedelta,
) -> list[LlmResponse]:
    """Streaming-aware model activity.

    Calls the LLM with ``stream=True`` and returns the collected list of
    raw ``LlmResponse`` chunks. The workflow's ``TemporalModel.generate_content_async``
    yields these to the caller.

    When ``streaming_event_topic`` is set, each response is also
    published to the workflow's pub/sub broker so external consumers
    (UIs, tracing, etc.) can observe responses as they arrive. Set the
    topic to ``None`` to skip publishing entirely; in that case no
    :class:`PubSubClient` is constructed.
    """
    if llm_request.model is None:
        raise ValueError("No model name provided, could not create LLM.")

    llm = LLMRegistry.new_llm(llm_request.model)
    if not llm:
        raise ValueError(f"Failed to create LLM for model: {llm_request.model}")

    responses: list[LlmResponse] = []

    async def consume(pubsub: PubSubClient | None, topic: str | None) -> None:
        async for response in llm.generate_content_async(
            llm_request=llm_request, stream=True
        ):
            activity.heartbeat()
            responses.append(response)
            if pubsub is not None and topic is not None:
                pubsub.publish(topic, response)

    if streaming_event_topic is None:
        await consume(None, None)
    else:
        pubsub = PubSubClient.from_activity(
            batch_interval=streaming_event_batch_interval,
        )
        async with pubsub:
            await consume(pubsub, streaming_event_topic)

    return responses


class TemporalModel(BaseLlm):
    """A Temporal-based LLM model that executes model invocations as activities."""

    def __init__(
        self,
        model_name: str,
        activity_config: ActivityConfig | None = None,
        *,
        summary_fn: Callable[[LlmRequest], str | None] | None = None,
        streaming_event_topic: str | None = "events",
        streaming_event_batch_interval: timedelta = timedelta(milliseconds=100),
    ) -> None:
        """Initialize the TemporalModel.

        Streaming is selected by the caller via the ADK
        ``generate_content_async(stream=True)`` argument; no plugin-level
        flag is needed.

        Args:
            model_name: The name of the model to use.
            activity_config: Configuration options for the activity execution.
            summary_fn: Optional callable that receives the LlmRequest and
                returns a summary string (or None) for the activity. Must be
                deterministic as it is called during workflow execution. If
                the callable raises, the exception will propagate and fail
                the workflow task.
            streaming_event_topic: Pub/sub topic to publish raw
                ``LlmResponse`` chunks to when streaming. Set to ``None``
                to skip publishing entirely (workflow-side iteration via
                ``stream=True`` still works, no broker required). When
                set, the workflow must host a
                :class:`temporalio.contrib.pubsub.PubSub` broker to
                receive the publishes; otherwise the signals are
                unhandled and dropped.
            streaming_event_batch_interval: Interval between automatic
                flushes for the pub/sub publisher used by the streaming
                activity. Ignored when ``streaming_event_topic`` is
                ``None``.

        Raises:
            ValueError: If both ``ActivityConfig["summary"]`` and ``summary_fn`` are set.
        """
        super().__init__(model=model_name)
        self._model_name = model_name
        self._summary_fn = summary_fn
        self._streaming_event_topic = streaming_event_topic
        self._streaming_event_batch_interval = streaming_event_batch_interval
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
            stream: Whether to use the streaming activity. When ``True``,
                each chunk is also published to ``streaming_event_topic``
                (if set) for external consumers.

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

        if stream:
            responses = await workflow.execute_activity(
                invoke_model_streaming,
                args=[
                    llm_request,
                    self._streaming_event_topic,
                    self._streaming_event_batch_interval,
                ],
                **config,
            )
        else:
            responses = await workflow.execute_activity(
                invoke_model,
                args=[llm_request],
                **config,
            )
        for response in responses:
            yield response
