"""Simple wrapper implementation using static activities for LangChain integration.

This module provides the core static activity pattern for the Temporal LangChain integration,
where all model and tool invocations are handled by two static activities rather than
creating unique activities for each wrapped component.
"""

import asyncio
import inspect
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, ConfigDict
from langchain_core.language_models.base import BaseLanguageModel
from langchain_core.tools.base import BaseTool
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables.config import RunnableConfig

from temporalio import activity, workflow


# Type aliases for better documentation
# Use Any instead of abstract classes to avoid Pydantic deserialization issues
from typing import Any as ModelData, Any as ToolData


# Monkey-patch for LangChain's run_in_executor to work in Temporal workflows
_original_run_in_executor = None


async def _temporal_run_in_executor(executor_or_config, func, *args, **kwargs):
    """
    Replacement for LangChain's run_in_executor that works in Temporal workflows.

    In Temporal workflows, we can't use real thread executors because they break
    determinism. Instead, we run the function synchronously.
    """
    try:
        # Try to detect if we're in a workflow context by checking for workflow module
        # If we can access workflow.info(), we're in a workflow
        try:
            workflow.info()
            # We're in a workflow context - run synchronously
            # Handle the context wrapper that LangChain uses
            if (
                hasattr(func, "func")
                and hasattr(func, "args")
                and hasattr(func, "keywords")
            ):
                # This is a partial function from LangChain's context wrapper
                actual_func = func.func
                actual_args = func.args + args
                actual_kwargs = {**func.keywords, **kwargs}
                result = actual_func(*actual_args, **actual_kwargs)
            else:
                # Regular function call
                result = func(*args, **kwargs)

            return result
        except Exception:
            # Not in workflow context - use original implementation
            pass
    except (ImportError, AttributeError):
        # Not in Temporal context at all
        pass

    # Fall back to original implementation
    if _original_run_in_executor:
        return await _original_run_in_executor(
            executor_or_config, func, *args, **kwargs
        )
    else:
        # Last resort - run synchronously
        # Handle the context wrapper that LangChain uses
        if (
            hasattr(func, "func")
            and hasattr(func, "args")
            and hasattr(func, "keywords")
        ):
            # This is a partial function from LangChain's context wrapper
            actual_func = func.func
            actual_args = func.args + args
            actual_kwargs = {**func.keywords, **kwargs}
            result = actual_func(*actual_args, **actual_kwargs)
        else:
            # Regular function call
            result = func(*args, **kwargs)

        return result


def _patch_langchain_executor():
    """Apply monkey-patch to LangChain's run_in_executor function."""
    global _original_run_in_executor

    from langchain_core.runnables import config as lc_config

    if not _original_run_in_executor:
        _original_run_in_executor = lc_config.run_in_executor
        lc_config.run_in_executor = _temporal_run_in_executor


# Apply the patch when this module is imported
_patch_langchain_executor()


# Input/Output types for static activities
class ModelCallInput(BaseModel):
    """Input for the langchain_model_call static activity."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    model_data: ModelData = Field(description="LangChain model object")
    model_type: str = Field(description="Fully qualified class name for validation")
    method_name: str = Field(description="Method to invoke (e.g., 'ainvoke')")
    args: List[Any] = Field(description="Positional arguments")
    kwargs: Dict[str, Any] = Field(description="Keyword arguments (callbacks stripped)")
    activity_callbacks: List[BaseCallbackHandler] = Field(
        default_factory=list, description="Callbacks for activity execution"
    )


class ToolCallInput(BaseModel):
    """Input for the langchain_tool_call static activity."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    tool_data: ToolData = Field(description="LangChain tool object")
    tool_type: str = Field(description="Fully qualified class name for validation")
    method_name: str = Field(description="Method to invoke (e.g., '_arun')")
    args: List[Any] = Field(description="Positional arguments")
    kwargs: Dict[str, Any] = Field(description="Keyword arguments (callbacks stripped)")
    activity_callbacks: List[BaseCallbackHandler] = Field(
        default_factory=list, description="Callbacks for activity execution"
    )


class CallOutput(BaseModel):
    """Output from static activities."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    result: Any = Field(description="Serialized return value")
    callback_events: List[Dict[str, Any]] = Field(
        default_factory=list, description="Captured callback events for replay"
    )


class ModelOutput(BaseModel):
    """Standardized output format for LangChain model responses."""

    content: str = Field(description="Main response content")
    tool_calls: Optional[List[Dict[str, Any]]] = Field(
        None, description="Tool calls made by model"
    )
    usage_metadata: Optional[Dict[str, Any]] = Field(
        None, description="Token usage and other metadata"
    )
    response_metadata: Optional[Dict[str, Any]] = Field(
        None, description="Provider-specific metadata"
    )

    @classmethod
    def from_langchain_response(cls, response: Any) -> "ModelOutput":
        """Convert a LangChain model response to ModelOutput format."""
        if hasattr(response, "content"):
            # AIMessage or similar
            return cls(
                content=str(response.content),
                tool_calls=getattr(response, "tool_calls", None),
                usage_metadata=getattr(response, "usage_metadata", None),
                response_metadata=getattr(response, "response_metadata", None),
            )
        elif isinstance(response, str):
            # Direct string response
            return cls(content=response)
        else:
            # Fallback - convert to string
            return cls(content=str(response))


# Callback handling utilities
class CallbackEventCapture(BaseCallbackHandler):
    """Captures callback events for replay in workflow."""

    def __init__(self):
        self.events: List[Dict[str, Any]] = []

    def on_llm_start(self, serialized, prompts, **kwargs):
        self.events.append(
            {
                "event": "on_llm_start",
                "serialized": serialized,
                "prompts": prompts,
                "kwargs": kwargs,
            }
        )

    def on_chat_model_start(self, serialized, messages, **kwargs):
        # Serialize complex objects to avoid replay issues
        def safe_serialize(obj):
            try:
                if hasattr(obj, "model_dump"):
                    return obj.model_dump()
                elif hasattr(obj, "dict"):
                    return obj.dict()
                elif isinstance(obj, list):
                    return [safe_serialize(item) for item in obj]
                else:
                    return obj
            except Exception:
                return str(obj) if obj is not None else None

        self.events.append(
            {
                "event": "on_chat_model_start",
                "serialized": safe_serialize(serialized),
                "messages": safe_serialize(messages),
                "kwargs": kwargs,
            }
        )

    def on_llm_new_token(self, token, **kwargs):
        self.events.append(
            {"event": "on_llm_new_token", "token": token, "kwargs": kwargs}
        )

    def on_llm_end(self, response, **kwargs):
        # Serialize response to avoid issues with Pydantic model methods during replay
        try:
            if hasattr(response, "model_dump"):
                # It's a Pydantic model, serialize it properly
                response_data = response.model_dump()
            elif hasattr(response, "dict"):
                # Legacy Pydantic v1 style
                response_data = response.dict()
            else:
                # Not a Pydantic model, keep as-is
                response_data = response
        except Exception:
            # Fallback: convert to basic dict if possible
            response_data = (
                dict(response) if hasattr(response, "__dict__") else response
            )

        self.events.append(
            {"event": "on_llm_end", "response": response_data, "kwargs": kwargs}
        )

    def on_llm_error(self, error, **kwargs):
        self.events.append({"event": "on_llm_error", "error": error, "kwargs": kwargs})

    def on_tool_start(self, serialized, input_str, **kwargs):
        self.events.append(
            {
                "event": "on_tool_start",
                "serialized": serialized,
                "input_str": input_str,
                "kwargs": kwargs,
            }
        )

    def on_tool_end(self, output, **kwargs):
        self.events.append({"event": "on_tool_end", "output": output, "kwargs": kwargs})

    def on_tool_error(self, error, **kwargs):
        self.events.append({"event": "on_tool_error", "error": error, "kwargs": kwargs})

    def on_text(self, text, **kwargs):
        self.events.append({"event": "on_text", "text": text, "kwargs": kwargs})


def get_callback_manager(callbacks: List[BaseCallbackHandler]):
    """Create a callback manager with activity callbacks and event capture."""
    from langchain_core.callbacks import CallbackManager

    # Add event capture callback to record events for workflow replay
    capture_callback = CallbackEventCapture()
    all_callbacks = callbacks + [capture_callback]

    return CallbackManager(all_callbacks), capture_callback


def extract_callback_events(
    capture_callback: CallbackEventCapture,
) -> List[Dict[str, Any]]:
    """Extract captured callback events."""
    return capture_callback.events


def _reconstruct_langchain_objects(obj):
    """Recursively reconstruct LangChain objects from serialized dicts.

    When LangChain objects like AIMessage, ChatResult, etc. go through Pydantic
    serialization, they become dicts. This function reconstructs them back to
    their proper LangChain types so the rest of the LangChain processing chain works.

    The goal is to eliminate dicts entirely and only return proper LangChain objects or primitives.
    """
    from langchain_core.outputs import (
        LLMResult,
        ChatResult,
        Generation,
        ChatGeneration,
    )
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    if isinstance(obj, dict):
        # First, try to reconstruct known LangChain object types

        # Check for LLMResult/ChatResult objects
        if "generations" in obj and isinstance(obj["generations"], list):
            # Determine if this is ChatResult or LLMResult based on structure
            generations = []
            is_chat_result = False

            for gen_list in obj["generations"]:
                gen_sublist = []
                if isinstance(gen_list, list):
                    for gen_dict in gen_list:
                        if isinstance(gen_dict, dict):
                            if "message" in gen_dict:
                                # This is a ChatGeneration
                                is_chat_result = True
                                message = _reconstruct_langchain_objects(
                                    gen_dict["message"]
                                )
                                generation = ChatGeneration(
                                    message=message,
                                    generation_info=gen_dict.get("generation_info"),
                                )
                                gen_sublist.append(generation)
                            elif "text" in gen_dict:
                                # This is a regular Generation
                                generation = Generation(
                                    text=gen_dict["text"],
                                    generation_info=gen_dict.get("generation_info"),
                                )
                                gen_sublist.append(generation)
                            else:
                                # Unknown generation format, keep as-is
                                gen_sublist.append(gen_dict)
                        else:
                            gen_sublist.append(gen_dict)
                else:
                    gen_sublist.append(gen_list)
                generations.append(gen_sublist)

            # Create the appropriate result type
            if is_chat_result:
                return ChatResult(
                    generations=generations,
                    llm_output=obj.get("llm_output"),
                    run=obj.get("run"),
                )
            else:
                return LLMResult(
                    generations=generations,
                    llm_output=obj.get("llm_output"),
                    run=obj.get("run"),
                )
        # Check for specific LangChain message types
        if obj.get("type") == "ai" and "content" in obj:
            # Reconstruct AIMessage
            return AIMessage(
                content=obj["content"],
                additional_kwargs=obj.get("additional_kwargs", {}),
                response_metadata=obj.get("response_metadata", {}),
                name=obj.get("name"),
                id=obj.get("id"),
                tool_calls=obj.get("tool_calls", []),
                invalid_tool_calls=obj.get("invalid_tool_calls", []),
                usage_metadata=obj.get("usage_metadata"),
            )

        elif obj.get("type") == "human" and "content" in obj:
            # Reconstruct HumanMessage
            return HumanMessage(
                content=obj["content"],
                additional_kwargs=obj.get("additional_kwargs", {}),
                response_metadata=obj.get("response_metadata", {}),
                name=obj.get("name"),
                id=obj.get("id"),
            )

        elif obj.get("type") == "system" and "content" in obj:
            # Reconstruct SystemMessage
            return SystemMessage(
                content=obj["content"],
                additional_kwargs=obj.get("additional_kwargs", {}),
                response_metadata=obj.get("response_metadata", {}),
                name=obj.get("name"),
                id=obj.get("id"),
            )

        # If no specific reconstruction worked, we need to decide what to do with this dict
        # The goal is to eliminate dicts entirely, so we either convert to a string or an object

        # Special case handling for simple content dicts that should become strings
        if len(obj) == 1 and "content" in obj and isinstance(obj["content"], str):
            # Return just the content string for Generation compatibility
            return obj["content"]

        # Check for text-like content that should be strings
        if len(obj) <= 3:
            # Try to extract meaningful text content for Generation compatibility
            for text_key in [
                "text",
                "content",
                "output",
                "response",
                "answer",
                "message",
            ]:
                if text_key in obj and isinstance(obj[text_key], str):
                    return obj[text_key]

            # If the dict only has string values, we might be able to use it as a single string
            string_values = [v for v in obj.values() if isinstance(v, str)]
            if len(string_values) == 1 and len(obj) == 1:
                return string_values[0]

        # Agent execution result handling
        if "output" in obj and isinstance(obj["output"], str) and len(obj) <= 4:
            # This handles agent executor results like {"input": "...", "output": "...", "chat_history": [...]}
            return obj["output"]

        # As a last resort, recursively process nested dicts but try to eliminate them
        reconstructed = {}
        for key, value in obj.items():
            reconstructed[key] = _reconstruct_langchain_objects(value)

        # If after reconstruction we still have a dict, we might need to convert it to a string
        # to prevent Generation errors. This is a safety measure.
        if isinstance(reconstructed, dict) and len(reconstructed) <= 2:
            # Try one more time to extract string content
            for text_key in ["content", "text", "output", "response"]:
                if text_key in reconstructed and isinstance(
                    reconstructed[text_key], str
                ):
                    return reconstructed[text_key]

        return reconstructed
    elif isinstance(obj, list):
        # Recursively process lists
        return [_reconstruct_langchain_objects(item) for item in obj]
    else:
        # Return primitive types as-is
        return obj


# Static activities
def _try_extract_input_from_dict(input_dict):
    """Try to extract a usable input from common LangChain dict patterns."""
    if not isinstance(input_dict, dict):
        return None

    # Pattern 1: Agent executor format {"input": "query", "chat_history": [...]}
    if "input" in input_dict:
        user_input = input_dict["input"]
        if isinstance(user_input, str):
            # Convert to messages format that most LLMs expect
            return [("human", user_input)]
        elif isinstance(user_input, list):
            # Already in messages format
            return user_input

    # Pattern 2: Direct prompt format {"messages": [...]}
    if "messages" in input_dict:
        messages = input_dict["messages"]
        # Fix tool messages that might be missing tool_call_id by matching with previous AI message tool calls
        fixed_messages = []
        available_tool_call_ids = []

        # First pass: collect available tool call IDs from AI messages
        for i, msg in enumerate(messages):
            if isinstance(msg, dict) and msg.get("type") == "ai":
                # Check for tool_calls in various locations (OpenAI stores them in additional_kwargs)
                tool_calls = []
                if "tool_calls" in msg:
                    tool_calls = msg.get("tool_calls", [])
                elif "additional_kwargs" in msg:
                    additional_kwargs = msg.get("additional_kwargs", {})
                    if "tool_calls" in additional_kwargs:
                        tool_calls = additional_kwargs.get("tool_calls", [])

                # Extract tool call IDs
                for tool_call in tool_calls:
                    if isinstance(tool_call, dict) and "id" in tool_call:
                        available_tool_call_ids.append(tool_call["id"])

        # Second pass: fix tool messages and assign proper tool_call_ids
        tool_call_index = 0
        for i, msg in enumerate(messages):
            if isinstance(msg, dict) and msg.get("type") == "tool":
                # Ensure tool messages have tool_call_id
                if "tool_call_id" not in msg:
                    msg = msg.copy()  # Don't modify original
                    # Use available tool call ID if we have one, otherwise create a fallback
                    if tool_call_index < len(available_tool_call_ids):
                        msg["tool_call_id"] = available_tool_call_ids[tool_call_index]
                    else:
                        msg["tool_call_id"] = f"tool_call_{i}"
                    tool_call_index += 1
            fixed_messages.append(msg)
        return fixed_messages

    # Pattern 3: Single text content {"content": "text"}
    if "content" in input_dict and isinstance(input_dict["content"], str):
        return [("human", input_dict["content"])]

    # Pattern 4: Simple text value for keys like "text", "query", "question"
    for key in ["text", "query", "question", "prompt"]:
        if key in input_dict and isinstance(input_dict[key], str):
            return [("human", input_dict[key])]

    return None  # Can't extract a usable input


@activity.defn(name="langchain_model_call")
async def langchain_model_call(input: ModelCallInput) -> CallOutput:
    """Execute a LangChain model method as a Temporal activity."""

    # 1. Reconstruct the model object if needed
    if isinstance(input.model_data, dict):
        # Model was serialized as dict, need to reconstruct it
        module_name, class_name = input.model_type.rsplit(".", 1)
        module = __import__(module_name, fromlist=[class_name])
        model_class = getattr(module, class_name)

        # Filter out parameters that the model class doesn't accept to avoid errors
        import inspect

        try:
            sig = inspect.signature(model_class.__init__)
            valid_params = {}

            # Check if the class accepts **kwargs
            has_var_keyword = any(
                p.kind == p.VAR_KEYWORD for p in sig.parameters.values()
            )

            for key, value in input.model_data.items():
                # Include parameter if it's explicitly in the signature or if the class accepts **kwargs
                if key in sig.parameters or has_var_keyword:
                    valid_params[key] = value
                # Skip parameters that don't exist and there's no **kwargs

            model = model_class(**valid_params)
        except Exception:
            # If parameter filtering fails, try with just the core parameters
            # This is a fallback for complex model classes
            core_params = {}
            for key, value in input.model_data.items():
                # Only include common LangChain model parameters
                if key in [
                    "model",
                    "model_name",
                    "temperature",
                    "max_tokens",
                    "api_key",
                    "base_url",
                ]:
                    core_params[key] = value

            try:
                model = model_class(**core_params)
            except Exception:
                # Last resort: try with no parameters
                model = model_class()
    else:
        # Model is already an object
        model = input.model_data

        # Validate the type matches
        if type(model).__module__ + "." + type(model).__qualname__ != input.model_type:
            raise ValueError(f"Model type mismatch: expected {input.model_type}")

    # 2. Set up callback manager with activity callbacks
    callback_manager, capture_callback = get_callback_manager(input.activity_callbacks)

    # 3. Execute method (handle sync via asyncio.to_thread)
    method = getattr(model, input.method_name)

    # Add callback manager to kwargs if available
    if callback_manager and "config" in input.kwargs:
        config = input.kwargs["config"]
        if config and isinstance(config, dict):
            config = config.copy()
            config["callbacks"] = callback_manager
            input.kwargs["config"] = config

    try:
        # Validate input arguments for invoke methods to catch dict inputs
        if input.method_name in ("ainvoke", "invoke") and input.args:
            first_arg = input.args[0]
            if isinstance(first_arg, dict):
                # Try to auto-convert common dict patterns
                converted_input = _try_extract_input_from_dict(first_arg)
                if converted_input is not None:
                    print(traceback.format_exc())
                    print(
                        f"WARNING: TemporalModelProxy auto-converted dict input {list(first_arg.keys())} to: {type(converted_input)}"
                    )
                    # Replace the first argument with the converted input
                    input = input.model_copy()
                    input.args = [converted_input] + list(input.args[1:])
                else:
                    # Can't convert, raise helpful error
                    raise ValueError(
                        f"Internal LangChain-Temporal compatibility issue: {type(first_arg)} with keys {list(first_arg.keys())} "
                        f"was passed to the model. This indicates that the LangChain agent chain "
                        f"(likely RunnableWithMessageHistory or AgentExecutor) is not properly formatting "
                        f"inputs before calling the model. The model should receive PromptValue, str, or list of BaseMessages, "
                        f"not the raw agent executor dict. This is a known issue with certain LangChain agent patterns "
                        f"when used with Temporal activities."
                    )

        if inspect.iscoroutinefunction(method):
            result = await method(*input.args, **input.kwargs)
        else:
            result = await asyncio.to_thread(method, *input.args, **input.kwargs)
    except Exception:
        # Capture callback events even on error
        callback_events = extract_callback_events(capture_callback)
        raise

    # 4. Capture callback events for workflow replay
    callback_events = extract_callback_events(capture_callback)

    return CallOutput(result=result, callback_events=callback_events)


@activity.defn(name="langchain_tool_call")
async def langchain_tool_call(input: ToolCallInput) -> CallOutput:
    """Execute a LangChain tool method as a Temporal activity."""

    # 1. Handle tool data which might be serialized as a dict
    tool = input.tool_data

    # If tool was serialized as a dict, we need to reconstruct it or call the original function
    if isinstance(tool, dict):
        # For @tool decorated functions, we need to call the original function directly
        # rather than trying to reconstruct the full StructuredTool
        module_name, class_name = input.tool_type.rsplit(".", 1)
        try:
            import importlib

            # Get the original function from the module
            if class_name == "StructuredTool":
                # This is a @tool decorated function - we need to find the original function
                # which should be in tests.contrib.langchain.smoke_activities based on the import
                tool_name = tool.get("name", "")

                # Try to find the function in the test module
                test_module_name = "tests.contrib.langchain.smoke_activities"
                try:
                    test_module = importlib.import_module(test_module_name)
                    if hasattr(test_module, tool_name):
                        langchain_tool = getattr(test_module, tool_name)

                        # For @tool decorated functions, we need to extract the actual function
                        # The @tool decorator creates a StructuredTool with a 'func' attribute for sync functions
                        # and 'coroutine' attribute for async functions
                        if (
                            hasattr(langchain_tool, "func")
                            and langchain_tool.func is not None
                        ):
                            original_func = langchain_tool.func
                        elif (
                            hasattr(langchain_tool, "coroutine")
                            and langchain_tool.coroutine is not None
                        ):
                            # For async functions, check coroutine attribute
                            original_func = langchain_tool.coroutine
                        elif hasattr(langchain_tool, "_run"):
                            # Try using the _run method directly
                            original_func = langchain_tool._run
                        else:
                            # Fallback: use the tool itself but need to handle argument format
                            original_func = langchain_tool

                        # Convert keyword arguments to positional if needed
                        # LangChain passes {'input': 3} but function expects magic_function(input=3)
                        if input.kwargs and "input" in input.kwargs and not input.args:
                            # Convert to positional argument
                            input.args = [input.kwargs.pop("input")]

                        # Call the original function directly
                        if original_func is None:
                            raise ValueError(
                                f"original_func is None for tool {tool_name}"
                            )

                        if inspect.iscoroutinefunction(original_func):
                            result = await original_func(*input.args, **input.kwargs)
                        else:
                            result = await asyncio.to_thread(
                                original_func, *input.args, **input.kwargs
                            )

                        # Return early since we called the function directly
                        # For direct function calls, we don't have callback capture set up
                        return CallOutput(result=result, callback_events=[])
                except ImportError:
                    pass

                raise ValueError(
                    f"Cannot find original function {tool_name} for StructuredTool"
                )
            else:
                # Regular tool class reconstruction
                tool_class = getattr(importlib.import_module(module_name), class_name)
                tool = tool_class(**tool)
        except (ImportError, AttributeError) as e:
            raise ValueError(f"Cannot reconstruct tool of type {input.tool_type}: {e}")

    # Validate the type matches (only if we didn't return early)
    if not isinstance(tool, dict):
        actual_type = type(tool).__module__ + "." + type(tool).__qualname__
        if actual_type != input.tool_type:
            raise ValueError(
                f"Tool type mismatch: expected {input.tool_type}, got {actual_type}"
            )

    # 2. Set up callback manager with activity callbacks
    callback_manager, capture_callback = get_callback_manager(input.activity_callbacks)

    # 3. Execute method (handle sync via asyncio.to_thread)
    method = getattr(tool, input.method_name)

    try:
        if inspect.iscoroutinefunction(method):
            # Handle LangChain tools that require config parameter
            if input.method_name == "_arun" and "config" not in input.kwargs:
                # Check if the method actually accepts a config parameter
                sig = inspect.signature(method)
                if "config" in sig.parameters:
                    input.kwargs["config"] = None

            result = await method(*input.args, **input.kwargs)
        else:
            # For sync methods like _run, filter kwargs to match method signature
            sig = inspect.signature(method)
            filtered_kwargs = {}
            for param_name, param_value in input.kwargs.items():
                if param_name in sig.parameters:
                    filtered_kwargs[param_name] = param_value
            result = await asyncio.to_thread(method, *input.args, **filtered_kwargs)
    except Exception:
        # Capture callback events even on error
        callback_events = extract_callback_events(capture_callback)
        raise

    # 4. Capture callback events for workflow replay
    callback_events = extract_callback_events(capture_callback)

    return CallOutput(result=result, callback_events=callback_events)


# Wrapper classes
class TemporalModelProxy(BaseLanguageModel):
    """Wrapper that proxies LangChain models to Temporal activities."""

    def __init__(
        self,
        model: BaseLanguageModel,
        workflow_callbacks: Optional[List[BaseCallbackHandler]] = None,
        **activity_params,
    ):
        """Initialize the proxy with a LangChain model and activity parameters."""
        # Validate model is a BaseLanguageModel subclass
        if not isinstance(model, BaseLanguageModel):
            raise ValueError(
                f"Model must be a subclass of BaseLanguageModel, got {type(model)}"
            )

        self._model = model
        self._activity_params = activity_params
        self._workflow_callbacks: List[BaseCallbackHandler] = workflow_callbacks or []
        self._binding_kwargs: Dict[str, Any] = {}

    @property
    def _llm_type(self) -> str:
        """Return type of language model."""
        return f"temporal_wrapped_{getattr(self._model, '_llm_type', 'unknown')}"

    def add_workflow_callback(self, callback: BaseCallbackHandler) -> None:
        """Add a callback to be executed in the workflow thread."""
        self._workflow_callbacks.append(callback)

    # Abstract method implementations
    def generate_prompt(self, prompts, stop=None, callbacks=None, **kwargs):
        """Generate method for prompts - not recommended in workflows."""
        raise NotImplementedError("Use async methods in workflows")

    async def agenerate_prompt(self, prompts, stop=None, callbacks=None, **kwargs):
        """Async generate method for prompts."""
        activity_input = ModelCallInput(
            model_data=self._model,
            model_type=f"{type(self._model).__module__}.{type(self._model).__qualname__}",
            method_name="agenerate_prompt",
            args=[prompts],
            kwargs={"stop": stop, "callbacks": callbacks, **kwargs},
            activity_callbacks=[],
        )

        output = await workflow.execute_activity(
            langchain_model_call, activity_input, **self._activity_params
        )
        return _reconstruct_langchain_objects(output.result)

    def predict(self, text, stop=None, **kwargs):
        """Predict method - not recommended in workflows."""
        raise NotImplementedError("Use async methods in workflows")

    async def apredict(self, text, stop=None, **kwargs):
        """Async predict method."""
        activity_input = ModelCallInput(
            model_data=self._model,
            model_type=f"{type(self._model).__module__}.{type(self._model).__qualname__}",
            method_name="apredict",
            args=[text],
            kwargs={"stop": stop, **kwargs},
            activity_callbacks=[],
        )

        output = await workflow.execute_activity(
            langchain_model_call, activity_input, **self._activity_params
        )
        return _reconstruct_langchain_objects(output.result)

    def predict_messages(self, messages, stop=None, **kwargs):
        """Predict messages method - not recommended in workflows."""
        raise NotImplementedError("Use async methods in workflows")

    async def apredict_messages(self, messages, stop=None, **kwargs):
        """Async predict messages method."""
        activity_input = ModelCallInput(
            model_data=self._model,
            model_type=f"{type(self._model).__module__}.{type(self._model).__qualname__}",
            method_name="apredict_messages",
            args=[messages],
            kwargs={"stop": stop, **kwargs},
            activity_callbacks=[],
        )

        output = await workflow.execute_activity(
            langchain_model_call, activity_input, **self._activity_params
        )
        return _reconstruct_langchain_objects(output.result)

    async def ainvoke(self, input, config: Optional[RunnableConfig] = None, **kwargs):
        """Main invocation method - runs model in activity."""

        # 1. Validate input type and transform if needed to handle LangChain compatibility issues
        validated_input = self._validate_input(input)

        # 2. Upsert search attributes before call (skip for now to avoid serialization issues)
        # await self._upsert_model_metadata()

        # 3. Split callbacks
        activity_callbacks, workflow_callbacks = self._split_callbacks(config)

        # 3. Prepare activity input
        # Merge binding kwargs with invocation kwargs
        merged_kwargs = {
            **self._binding_kwargs,
            **kwargs,
            "config": self._strip_callbacks(config),
        }

        activity_input = ModelCallInput(
            model_data=self._model,
            model_type=f"{type(self._model).__module__}.{type(self._model).__qualname__}",
            method_name="ainvoke",
            args=[validated_input],  # Use the validated/transformed input
            kwargs=merged_kwargs,
            activity_callbacks=activity_callbacks,
        )

        # 4. Execute activity
        output = await workflow.execute_activity(
            langchain_model_call, activity_input, **self._activity_params
        )

        # 5. Replay callback events in workflow
        await self._replay_callbacks(workflow_callbacks, output.callback_events)

        # 6. Reconstruct LangChain objects from serialized dicts
        # This is necessary because Pydantic serialization converts LangChain objects
        # like AIMessage, ChatResult, etc. to dicts, but LangChain code expects the actual objects
        reconstructed_result = _reconstruct_langchain_objects(output.result)

        # Check if we're still returning a dict - this should not happen with proper reconstruction
        if isinstance(reconstructed_result, dict):
            workflow.logger.error(
                f"BUG: Still returning dict with keys: {list(reconstructed_result.keys())} - this will cause Generation errors. Our reconstruction is incomplete."
            )

            # Emergency fallback: extract content to prevent Generation errors
            for text_key in [
                "content",
                "text",
                "output",
                "response",
                "message",
                "answer",
            ]:
                if text_key in reconstructed_result and isinstance(
                    reconstructed_result[text_key], str
                ):
                    workflow.logger.error(
                        f"Emergency fallback: extracting '{text_key}' = '{reconstructed_result[text_key]}' from dict"
                    )
                    return reconstructed_result[text_key]

            # Last resort: convert to JSON string
            import json

            json_str = json.dumps(reconstructed_result, default=str)
            workflow.logger.error(
                f"Last resort: converting dict to JSON string: {json_str}"
            )
            return json_str

        return reconstructed_result

    def invoke(self, input, config: Optional[RunnableConfig] = None, **kwargs):
        """Synchronous invoke - not recommended in workflows."""
        raise NotImplementedError("Use async methods (ainvoke) in workflows")

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """Generate method - not used in async workflows."""
        raise NotImplementedError("Use async methods in workflows")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        """Async generate method - delegates to wrapped model via activity."""
        # Merge binding kwargs with generate kwargs
        merged_kwargs = {
            **self._binding_kwargs,
            "stop": stop,
            "run_manager": run_manager,
            **kwargs,
        }

        activity_input = ModelCallInput(
            model_data=self._model,
            model_type=f"{type(self._model).__module__}.{type(self._model).__qualname__}",
            method_name="_agenerate",
            args=[messages],
            kwargs=merged_kwargs,
            activity_callbacks=[],
        )

        output = await workflow.execute_activity(
            langchain_model_call, activity_input, **self._activity_params
        )
        return _reconstruct_langchain_objects(output.result)

    def __getattr__(self, name: str) -> Any:
        """Forward all other method calls to the wrapped model via activity."""
        attr = getattr(self._model, name)

        if callable(attr):

            async def wrapped_method(*args, **kwargs):
                # Add input validation for methods that need it
                if name in ("ainvoke", "invoke") and args:
                    # Validate and potentially transform the first argument (input) for invoke methods
                    validated_input = self._validate_input(args[0])
                    args = [validated_input] + list(
                        args[1:]
                    )  # Replace first arg with validated input

                activity_input = ModelCallInput(
                    model_data=self._model,
                    model_type=f"{type(self._model).__module__}.{type(self._model).__qualname__}",
                    method_name=name,
                    args=list(args),
                    kwargs=kwargs,
                    activity_callbacks=[],
                )

                output = await workflow.execute_activity(
                    langchain_model_call, activity_input, **self._activity_params
                )
                return _reconstruct_langchain_objects(output.result)

            return wrapped_method

        # For non-callable attributes, return as-is (may be expensive!)
        return attr

    def bind(self, **kwargs):
        """Bind parameters to the model."""
        bound_model = self._model.bind(**kwargs)

        # Handle RunnableBinding - extract the original model and merge kwargs
        if hasattr(bound_model, "bound") and hasattr(bound_model, "kwargs"):
            # Extract the original model from the RunnableBinding
            original_model = bound_model.bound
            # Merge the binding kwargs with any existing kwargs from this proxy and new kwargs
            merged_kwargs = {
                **self._binding_kwargs,
                **getattr(bound_model, "kwargs", {}),
                **kwargs,
            }

            # Create a new proxy with the original model and merged kwargs
            proxy = TemporalModelProxy(
                original_model,
                workflow_callbacks=self._workflow_callbacks,
                **self._activity_params,
            )
            # Store the merged kwargs for later use
            proxy._binding_kwargs = merged_kwargs
            return proxy
        else:
            # For models that don't create RunnableBinding, proceed normally
            return TemporalModelProxy(
                bound_model,
                workflow_callbacks=self._workflow_callbacks,
                **self._activity_params,
            )

    def bind_tools(self, tools: List[Any], **kwargs):
        """Bind tools to the model."""
        bound_model = self._model.bind_tools(tools, **kwargs)

        # Handle RunnableBinding - extract the original model and merge kwargs
        if hasattr(bound_model, "bound") and hasattr(bound_model, "kwargs"):
            # Extract the original model from the RunnableBinding
            original_model = bound_model.bound
            # Merge the binding kwargs with any existing kwargs from this proxy and new kwargs
            merged_kwargs = {
                **self._binding_kwargs,
                **getattr(bound_model, "kwargs", {}),
                **kwargs,
            }

            # Create a new proxy with the original model and merged kwargs
            proxy = TemporalModelProxy(
                original_model,
                workflow_callbacks=self._workflow_callbacks,
                **self._activity_params,
            )
            # Store the merged kwargs for later use
            proxy._binding_kwargs = merged_kwargs
            return proxy
        else:
            # For models that don't create RunnableBinding, proceed normally
            return TemporalModelProxy(
                bound_model,
                workflow_callbacks=self._workflow_callbacks,
                **self._activity_params,
            )

    async def _upsert_model_metadata(self):
        """Upsert search attributes for model tracking."""
        model_name = getattr(self._model, "model_name", type(self._model).__name__)
        # Ensure model_name is a string and format for Temporal search attributes
        model_name_str = str(model_name) if model_name else "unknown"
        # upsert_search_attributes returns None, so don't await it
        workflow.upsert_search_attributes(
            {
                "llm.model_name": [
                    model_name_str
                ]  # Search attributes need to be in list format
            }
        )

    def _split_callbacks(
        self, config: Optional[RunnableConfig]
    ) -> Tuple[List[BaseCallbackHandler], List[BaseCallbackHandler]]:
        """Split callbacks into activity and workflow callbacks."""
        if not config or not config.get("callbacks"):
            return [], self._workflow_callbacks

        # Handle both CallbackManager and list of callbacks
        callbacks = config["callbacks"]
        if hasattr(callbacks, "handlers"):
            # It's a CallbackManager, extract handlers
            raw_callbacks = callbacks.handlers
        elif isinstance(callbacks, list):
            # It's already a list of callbacks
            raw_callbacks = callbacks
        else:
            # Single callback, wrap in list
            raw_callbacks = [callbacks]

        # IMPORTANT: Don't send any callbacks to activities!
        # Callbacks cannot be properly serialized and activities run in separate
        # processes where callback objects won't work correctly.
        # Instead, all callbacks should stay in the workflow and receive
        # callback events via the CallOutput.callback_events mechanism.
        activity_callbacks = []

        # All config callbacks become workflow callbacks
        all_workflow_callbacks = self._workflow_callbacks + raw_callbacks

        return activity_callbacks, all_workflow_callbacks

    def _strip_callbacks(
        self, config: Optional[RunnableConfig]
    ) -> Optional[RunnableConfig]:
        """Remove callbacks from config to avoid sending them to activity."""
        if not config:
            return config

        # Create a copy of config without callbacks
        stripped_config = config.copy() if config else {}
        if "callbacks" in stripped_config:
            del stripped_config["callbacks"]

        return stripped_config

    def _validate_input(self, input):
        """Validate input type to catch common errors early."""
        if isinstance(input, dict):
            # This is an internal LangChain agent compatibility issue
            # The agent chain is passing a dict to the model when it should pass formatted messages
            # This happens with RunnableWithMessageHistory + structured agents

            # Try to extract a usable input from common dict patterns
            extracted_input = self._try_extract_input_from_dict(input)
            if extracted_input is not None:
                # Log the transformation for debugging
                print(
                    f"WARNING: TemporalModelProxy auto-converted dict input {list(input.keys())} to: {type(extracted_input)}"
                )
                return extracted_input

            # If we can't extract a usable input, raise an error
            raise ValueError(
                f"Internal LangChain-Temporal compatibility issue: {type(input)} with keys {list(input.keys())} "
                f"was passed to the model. This indicates that the LangChain agent chain "
                f"(likely RunnableWithMessageHistory or AgentExecutor) is not properly formatting "
                f"inputs before calling the model. The model should receive PromptValue, str, or list of BaseMessages, "
                f"not the raw agent executor dict. This is a known issue with certain LangChain agent patterns "
                f"when used with Temporal activities."
            )

        return input  # Return the input unchanged if it's not a dict

    def _try_extract_input_from_dict(self, input_dict):
        """Try to extract a usable input from common LangChain dict patterns."""
        if not isinstance(input_dict, dict):
            return None

        # Pattern 1: Agent executor format {"input": "query", "chat_history": [...]}
        if "input" in input_dict:
            user_input = input_dict["input"]
            if isinstance(user_input, str):
                # Convert to messages format that most LLMs expect
                return [("human", user_input)]
            elif isinstance(user_input, list):
                # Already in messages format
                return user_input

        # Pattern 2: Direct prompt format {"messages": [...]}
        if "messages" in input_dict:
            return input_dict["messages"]

        # Pattern 3: Single text content {"content": "text"}
        if "content" in input_dict and isinstance(input_dict["content"], str):
            return [("human", input_dict["content"])]

        # Pattern 4: Simple text value for keys like "text", "query", "question"
        for key in ["text", "query", "question", "prompt"]:
            if key in input_dict and isinstance(input_dict[key], str):
                return [("human", input_dict[key])]

        return None  # Can't extract a usable input

    async def _replay_callbacks(
        self, callbacks: List[BaseCallbackHandler], events: List[Dict[str, Any]]
    ):
        """Replay callback events in the workflow thread."""
        for event in events:
            for callback in callbacks:
                method = getattr(callback, event["event"], None)
                if method:
                    # Reconstruct the original method call with proper positional arguments
                    event_name = event["event"]
                    kwargs = event.get("kwargs", {})

                    # Skip problematic callback events that can't be properly serialized/deserialized
                    # These events contain complex LangChain objects that lose their methods during serialization
                    if event_name in ["on_llm_end", "on_chat_model_start"] and any(
                        isinstance(event.get(key), dict)
                        for key in ["response", "messages", "serialized"]
                    ):
                        # Skip callbacks that would fail due to serialization issues
                        continue

                    try:
                        if event_name == "on_llm_start":
                            args = [event.get("serialized"), event.get("prompts")]
                        elif event_name == "on_chat_model_start":
                            args = [event.get("serialized"), event.get("messages")]
                        elif event_name == "on_llm_new_token":
                            args = [event.get("token")]
                        elif event_name == "on_llm_end":
                            # Try to reconstruct LangChain objects for callback replay
                            response = event.get("response")
                            if isinstance(response, dict):
                                # Attempt to reconstruct the LLMResult object if possible
                                try:
                                    from langchain_core.outputs import LLMResult

                                    # Try to create a basic LLMResult from the dict data
                                    if "generations" in response:
                                        # Reconstruct as LLMResult
                                        reconstructed = LLMResult(
                                            generations=response.get("generations", []),
                                            llm_output=response.get("llm_output", {}),
                                            run=response.get("run", []),
                                        )
                                        args = [reconstructed]
                                    else:
                                        # Can't reconstruct, skip this callback to avoid errors
                                        continue
                                except ImportError:
                                    # LangChain not available, skip callback
                                    continue
                                except Exception:
                                    # Reconstruction failed, skip callback to avoid errors
                                    continue
                            else:
                                args = [response]
                        elif event_name == "on_llm_error":
                            args = [event.get("error")]
                        elif event_name == "on_tool_start":
                            args = [event.get("serialized"), event.get("input_str")]
                        elif event_name == "on_tool_end":
                            args = [event.get("output")]
                        elif event_name == "on_tool_error":
                            args = [event.get("error")]
                        elif event_name == "on_text":
                            args = [event.get("text")]
                        else:
                            # Unknown event, just use kwargs
                            args = []

                        # Execute in workflow thread - safe for deterministic operations
                        if asyncio.iscoroutinefunction(method):
                            await method(*args, **kwargs)
                        else:
                            method(*args, **kwargs)
                    except Exception as e:
                        # Log callback replay errors but don't fail the workflow
                        # Check if this is the known model_dump issue and provide more context
                        error_str = str(e)
                        if "model_dump" in error_str:
                            workflow.logger.warning(
                                f"Failed to replay callback {event_name}: {e} (serialized object passed to callback expecting Pydantic model)"
                            )
                        else:
                            workflow.logger.warning(
                                f"Failed to replay callback {event_name}: {e}"
                            )


class TemporalToolProxy(BaseTool):
    """Wrapper that proxies LangChain tools to Temporal activities."""

    def __init__(
        self,
        tool: BaseTool,
        workflow_callbacks: Optional[List[BaseCallbackHandler]] = None,
        **activity_params,
    ):
        """Initialize the proxy with a LangChain tool and activity parameters."""
        # Initialize BaseTool with tool properties first
        super().__init__(
            name=tool.name,
            description=tool.description,
            args_schema=getattr(tool, "args_schema", None),
            return_direct=getattr(tool, "return_direct", False),
            verbose=getattr(tool, "verbose", False),
        )

        # Store private attributes using object.__setattr__ to bypass Pydantic
        object.__setattr__(self, "_tool", tool)
        object.__setattr__(self, "_activity_params", activity_params)
        object.__setattr__(self, "_workflow_callbacks", workflow_callbacks or [])

    def add_workflow_callback(self, callback: BaseCallbackHandler) -> None:
        """Add a callback to be executed in the workflow thread."""
        self._workflow_callbacks.append(callback)

    async def _arun(self, *args, **kwargs) -> str:
        """Async run method - delegates to wrapped tool via activity."""
        # Determine which method to call - prefer _arun, fallback to _run
        method_name = "_arun" if hasattr(self._tool, "_arun") else "_run"

        # Create a minimal serializable representation of the tool
        # Only include basic metadata needed for reconstruction, exclude functions and model classes
        tool_for_serialization = {
            "name": getattr(self._tool, "name", ""),
            "description": getattr(self._tool, "description", ""),
            "return_direct": getattr(self._tool, "return_direct", False),
            "verbose": getattr(self._tool, "verbose", False),
            "args_schema": None,  # Explicitly set to None to avoid serialization of model classes
        }

        activity_input = ToolCallInput(
            tool_data=tool_for_serialization,
            tool_type=f"{type(self._tool).__module__}.{type(self._tool).__qualname__}",
            method_name=method_name,
            args=list(args),
            kwargs=kwargs,
            activity_callbacks=[],
        )

        output = await workflow.execute_activity(
            langchain_tool_call, activity_input, **self._activity_params
        )
        return str(output.result)

    def _run(self, *args, **kwargs) -> str:
        """Synchronous run method - not directly usable in workflow context."""
        raise NotImplementedError(
            "Synchronous _run method cannot be called from workflow context. "
            "LangChain agents should use the async _arun method instead. "
            "The underlying tool's sync method will be executed via asyncio.to_thread "
            "in the activity implementation."
        )

    async def ainvoke(self, input: str, config: Optional[Dict] = None, **kwargs) -> str:
        """Async invoke method - delegates to _arun."""
        return await self._arun(input, **kwargs)


# Public API functions
def simple_model_as_activity(
    model: BaseLanguageModel,
    workflow_callbacks: Optional[List[BaseCallbackHandler]] = None,
    **activity_params,
) -> TemporalModelProxy:
    """Wrap a LangChain model as a Temporal activity."""
    return TemporalModelProxy(model, workflow_callbacks, **activity_params)


def simple_tool_as_activity(
    tool: BaseTool,
    workflow_callbacks: Optional[List[BaseCallbackHandler]] = None,
    **activity_params,
) -> TemporalToolProxy:
    """Wrap a LangChain tool as a Temporal activity."""
    return TemporalToolProxy(tool, workflow_callbacks, **activity_params)


def get_simple_wrapper_activities() -> List[Callable]:
    """Return static activities for worker registration."""
    return [langchain_model_call, langchain_tool_call]
