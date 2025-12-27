"""Temporal-wrapped LangChain chat models for durable execution."""

from __future__ import annotations

from datetime import timedelta
from typing import (
    TYPE_CHECKING,
    Any,
    List,
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
    """Internal wrapper that delegates chat model calls to activities."""

    def __init__(
        self,
        model: Union[str, "BaseChatModel"],
        *,
        start_to_close_timeout: timedelta,
        schedule_to_close_timeout: timedelta | None = None,
        schedule_to_start_timeout: timedelta | None = None,
        heartbeat_timeout: timedelta | None = None,
        task_queue: str | None = None,
        retry_policy: "RetryPolicy | None" = None,
        cancellation_type: "ActivityCancellationType | None" = None,
        versioning_intent: "VersioningIntent | None" = None,
        priority: "Priority | None" = None,
    ) -> None:
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
        """Create a dynamic BaseChatModel subclass wrapping the original model."""
        # Import here to avoid workflow sandbox issues
        with workflow.unsafe.imports_passed_through():
            from langchain_core.language_models.chat_models import BaseChatModel
            from langchain_core.outputs import ChatGeneration, ChatResult

        original_model = self._model
        activity_options = self._activity_options

        # Get model name for activity
        if isinstance(original_model, str):
            model_name: str | None = original_model
            model_instance: BaseChatModel | None = None
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
                stop: List[str] | None = None,
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
                stop: List[str] | None = None,
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
    schedule_to_close_timeout: timedelta | None = None,
    schedule_to_start_timeout: timedelta | None = None,
    heartbeat_timeout: timedelta | None = None,
    task_queue: str | None = None,
    retry_policy: "RetryPolicy | None" = None,
    cancellation_type: "ActivityCancellationType | None" = None,
    versioning_intent: "VersioningIntent | None" = None,
    priority: "Priority | None" = None,
) -> "BaseChatModel":
    """Wrap a LangChain chat model to execute LLM calls as Temporal activities.

    .. warning::
        This API is experimental and may change in future versions.

    Each LLM invocation becomes a separate activity with durability and retries.
    The wrapped model preserves the BaseChatModel interface.
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
