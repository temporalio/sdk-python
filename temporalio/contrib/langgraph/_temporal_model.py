"""Temporal-wrapped LangChain chat models for durable execution.

This module provides the temporal_model() wrapper that converts LangChain
chat models to execute LLM calls as Temporal activities, enabling durable
model execution within workflow-executed agentic nodes.
"""

from __future__ import annotations

from datetime import timedelta
from typing import (
    TYPE_CHECKING,
    Any,
    List,
    Optional,
    Sequence,
    Union,
)

from temporalio import workflow

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import BaseMessage
    from langchain_core.outputs import ChatResult

    from temporalio.common import Priority, RetryPolicy
    from temporalio.workflow import ActivityCancellationType, VersioningIntent


class _TemporalChatModel:
    """Internal wrapper that delegates chat model calls to activities.

    This class creates a BaseChatModel subclass that routes LLM calls through
    Temporal activities when running inside a workflow.
    """

    def __init__(
        self,
        model: Union[str, "BaseChatModel"],
        *,
        start_to_close_timeout: timedelta,
        schedule_to_close_timeout: Optional[timedelta] = None,
        schedule_to_start_timeout: Optional[timedelta] = None,
        heartbeat_timeout: Optional[timedelta] = None,
        task_queue: Optional[str] = None,
        retry_policy: Optional["RetryPolicy"] = None,
        cancellation_type: Optional["ActivityCancellationType"] = None,
        versioning_intent: Optional["VersioningIntent"] = None,
        priority: Optional["Priority"] = None,
    ) -> None:
        """Initialize the temporal model wrapper.

        Args:
            model: Model name string or BaseChatModel instance.
            start_to_close_timeout: Timeout for each LLM call activity.
            schedule_to_close_timeout: Total time from scheduling to completion.
            schedule_to_start_timeout: Time from scheduling until start.
            heartbeat_timeout: Heartbeat interval for long-running calls.
            task_queue: Route to specific workers.
            retry_policy: Temporal retry policy for failures.
            cancellation_type: How cancellation is handled.
            versioning_intent: Worker versioning intent.
            priority: Task priority.
        """
        self._model = model
        self._activity_options: dict[str, Any] = {
            "start_to_close_timeout": start_to_close_timeout,
        }
        if schedule_to_close_timeout is not None:
            self._activity_options["schedule_to_close_timeout"] = (
                schedule_to_close_timeout
            )
        if schedule_to_start_timeout is not None:
            self._activity_options["schedule_to_start_timeout"] = (
                schedule_to_start_timeout
            )
        if heartbeat_timeout is not None:
            self._activity_options["heartbeat_timeout"] = heartbeat_timeout
        if task_queue is not None:
            self._activity_options["task_queue"] = task_queue
        if retry_policy is not None:
            self._activity_options["retry_policy"] = retry_policy
        if cancellation_type is not None:
            self._activity_options["cancellation_type"] = cancellation_type
        if versioning_intent is not None:
            self._activity_options["versioning_intent"] = versioning_intent
        if priority is not None:
            self._activity_options["priority"] = priority

    def _create_wrapper_class(self) -> type:
        """Create a dynamic BaseChatModel subclass that wraps the original model."""
        # Import here to avoid workflow sandbox issues
        with workflow.unsafe.imports_passed_through():
            from langchain_core.language_models.chat_models import BaseChatModel
            from langchain_core.outputs import ChatGeneration, ChatResult

        original_model = self._model
        activity_options = self._activity_options

        # Get model name for activity
        if isinstance(original_model, str):
            model_name: Optional[str] = original_model
            model_instance: Optional[BaseChatModel] = None
        else:
            model_name = getattr(original_model, "model_name", None) or getattr(
                original_model, "model", None
            )
            model_instance = original_model

        class TemporalChatModelWrapper(BaseChatModel):  # type: ignore[misc]
            """Dynamic wrapper class for temporal chat model execution."""

            # Store references as class attributes - use Any to avoid Pydantic validation
            # issues with non-Pydantic types being passed
            _temporal_model_name: Any = model_name
            _temporal_model_instance: Any = model_instance
            _temporal_activity_options: Any = activity_options

            @property
            def _llm_type(self) -> str:
                """Return type of chat model."""
                return "temporal-chat-model"

            @property
            def _identifying_params(self) -> dict[str, Any]:
                """Return identifying parameters."""
                return {"model_name": self._temporal_model_name}

            def _generate(
                self,
                messages: List["BaseMessage"],
                stop: Optional[List[str]] = None,
                run_manager: Any = None,
                **kwargs: Any,
            ) -> "ChatResult":
                """Synchronous generation - delegates to async."""
                import asyncio

                return asyncio.get_event_loop().run_until_complete(
                    self._agenerate(
                        messages, stop=stop, run_manager=run_manager, **kwargs
                    )
                )

            async def _agenerate(  # type: ignore[override]
                self,
                messages: List["BaseMessage"],
                stop: Optional[List[str]] = None,
                run_manager: Any = None,
                **kwargs: Any,
            ) -> "ChatResult":
                """Async generation - routes to activity when in workflow."""
                # Check if we're in a workflow
                if not workflow.in_workflow():
                    # Outside workflow, use model directly
                    if self._temporal_model_instance is not None:
                        return await self._temporal_model_instance._agenerate(
                            messages, stop=stop, run_manager=run_manager, **kwargs
                        )
                    else:
                        raise RuntimeError(
                            "Cannot invoke temporal_model outside of a workflow "
                            "when initialized with a model name string. "
                            "Either use inside a workflow or pass a model instance."
                        )

                # In workflow, execute as activity
                with workflow.unsafe.imports_passed_through():
                    from temporalio.contrib.langgraph._activities import (
                        execute_chat_model,
                    )
                    from temporalio.contrib.langgraph._models import (
                        ChatModelActivityInput,
                    )

                # Serialize messages for activity
                serialized_messages = [
                    msg.model_dump()
                    if hasattr(msg, "model_dump")
                    else {"content": str(msg)}
                    for msg in messages
                ]

                activity_input = ChatModelActivityInput(
                    model_name=self._temporal_model_name,
                    messages=serialized_messages,
                    stop=stop,
                    kwargs=kwargs,
                )

                # Execute as activity
                result = await workflow.execute_activity(
                    execute_chat_model,
                    activity_input,
                    **self._temporal_activity_options,
                )

                # Convert result back to ChatResult
                generations = []
                for gen_data in result.generations:
                    # Reconstruct message from serialized form
                    with workflow.unsafe.imports_passed_through():
                        from langchain_core.messages import AIMessage

                    message = AIMessage(**gen_data["message"])
                    generations.append(
                        ChatGeneration(
                            message=message,
                            generation_info=gen_data.get("generation_info"),
                        )
                    )

                return ChatResult(
                    generations=generations,
                    llm_output=result.llm_output,
                )

            def bind_tools(
                self,
                tools: Sequence[Any],
                **kwargs: Any,
            ) -> "TemporalChatModelWrapper":
                """Bind tools to the model.

                This stores the tools for use in the activity, where they will
                be bound to the actual model instance.
                """
                # For now, we don't support tool binding in the wrapper
                # Users should bind tools to the underlying model before wrapping
                raise NotImplementedError(
                    "Tool binding on temporal_model is not yet supported. "
                    "Please bind tools to the model before wrapping with temporal_model(), "
                    "or use temporal_tool() for individual tool execution."
                )

        return TemporalChatModelWrapper

    def wrap(self) -> "BaseChatModel":
        """Create and return the wrapped model instance."""
        wrapper_class = self._create_wrapper_class()
        return wrapper_class()  # type: ignore[return-value]


def temporal_model(
    model: Union[str, "BaseChatModel"],
    *,
    start_to_close_timeout: timedelta = timedelta(minutes=2),
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    task_queue: Optional[str] = None,
    retry_policy: Optional["RetryPolicy"] = None,
    cancellation_type: Optional["ActivityCancellationType"] = None,
    versioning_intent: Optional["VersioningIntent"] = None,
    priority: Optional["Priority"] = None,
) -> "BaseChatModel":
    """Wrap a LangChain chat model to execute LLM calls as Temporal activities.

    Use this when running agentic nodes (like create_react_agent) in the
    workflow with run_in_workflow=True. Each LLM invocation becomes a separate
    activity, providing durability and retryability for each turn in the
    agentic loop.

    The wrapped model preserves the interface of BaseChatModel, so it works
    seamlessly with LangChain agents and the LangGraph framework.

    Args:
        model: Model name string (e.g., "gpt-4o", "claude-3-opus") or a
            BaseChatModel instance. If a string, the model will be instantiated
            in the activity using the model registry.
        start_to_close_timeout: Timeout for each LLM call activity.
            Defaults to 2 minutes.
        schedule_to_close_timeout: Total time allowed from scheduling to
            completion, including retries.
        schedule_to_start_timeout: Maximum time from scheduling until the
            activity starts executing on a worker.
        heartbeat_timeout: Maximum time between heartbeat requests. The
            activity automatically heartbeats during LLM calls.
        task_queue: Route LLM calls to a specific task queue (e.g., workers
            with GPU or specific API keys). If None, uses the workflow's
            task queue.
        retry_policy: Temporal retry policy for transient failures (e.g.,
            rate limits, temporary API errors).
        cancellation_type: How cancellation of LLM calls is handled.
        versioning_intent: Whether to run on a compatible worker Build ID.
        priority: Priority for task queue ordering.

    Returns:
        A wrapped BaseChatModel that executes LLM calls as Temporal activities
        when invoked within a workflow.

    Example:
        Basic usage with model name:

            >>> from temporalio.contrib.langgraph import temporal_model
            >>> from langgraph.prebuilt import create_react_agent
            >>>
            >>> model = temporal_model(
            ...     "gpt-4o",
            ...     start_to_close_timeout=timedelta(minutes=2),
            ...     retry_policy=RetryPolicy(maximum_attempts=3),
            ... )
            >>>
            >>> agent = create_react_agent(model, tools)

        With model instance:

            >>> from langchain_openai import ChatOpenAI
            >>>
            >>> base_model = ChatOpenAI(model="gpt-4o", temperature=0)
            >>> model = temporal_model(
            ...     base_model,
            ...     start_to_close_timeout=timedelta(minutes=5),
            ... )

        With heartbeat for long inference:

            >>> model = temporal_model(
            ...     "claude-3-opus",
            ...     start_to_close_timeout=timedelta(minutes=10),
            ...     heartbeat_timeout=timedelta(seconds=30),
            ... )

        Complete pattern with react_agent:

            >>> from temporalio.contrib.langgraph import (
            ...     temporal_model,
            ...     temporal_tool,
            ...     node_activity_options,
            ... )
            >>>
            >>> # Durable model
            >>> model = temporal_model("gpt-4o")
            >>>
            >>> # Durable tools
            >>> tools = [temporal_tool(search_web), calculator]
            >>>
            >>> # Create react agent
            >>> agent = create_react_agent(model, tools)
            >>>
            >>> # Add to graph with workflow execution
            >>> graph.add_node(
            ...     "agent",
            ...     agent,
            ...     metadata=node_activity_options(run_in_workflow=True),
            ... )

    Note:
        When using a model name string, you must register a model factory
        with the model registry. See `register_model_factory()` for details.
    """
    # Register model if it's an instance
    if not isinstance(model, str):
        from temporalio.contrib.langgraph._model_registry import register_model

        register_model(model)

    # Create and return wrapper
    wrapper = _TemporalChatModel(
        model,
        start_to_close_timeout=start_to_close_timeout,
        schedule_to_close_timeout=schedule_to_close_timeout,
        schedule_to_start_timeout=schedule_to_start_timeout,
        heartbeat_timeout=heartbeat_timeout,
        task_queue=task_queue,
        retry_policy=retry_policy,
        cancellation_type=cancellation_type,
        versioning_intent=versioning_intent,
        priority=priority,
    )

    return wrapper.wrap()
