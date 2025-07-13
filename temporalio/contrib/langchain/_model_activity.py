"""A temporal activity that invokes LangChain models.

Implements mapping of LangChain datastructures to Pydantic friendly types.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from pydantic import BaseModel
from typing_extensions import Required, TypedDict

from temporalio import activity


@dataclass
class LangChainToolInput:
    """Data conversion friendly representation of a LangChain tool."""

    name: str
    description: str
    args_schema: Optional[Dict[str, Any]] = None


class ActivityModelInput(TypedDict, total=False):
    """Input for the invoke_model_activity activity."""

    model_name: Optional[str]
    messages: Required[List[Dict[str, Any]]]  # List of message dicts
    tools: List[LangChainToolInput]
    temperature: Optional[float]
    max_tokens: Optional[int]
    model_kwargs: Dict[str, Any]


class ModelOutput(BaseModel):
    """Output from the model activity."""

    content: str
    tool_calls: Optional[List[Dict[str, Any]]] = None
    usage: Optional[Dict[str, Any]] = None


class ModelActivity:
    """Class wrapper for LangChain model invocation activities."""

    def __init__(self, model: Optional[Any] = None):
        """Initialize the activity with a LangChain model.

        Args:
            model: A LangChain model instance (e.g., ChatOpenAI, ChatAnthropic, etc.)
        """
        self._model = model

    def set_model(self, model: Any) -> None:
        """Set or update the model instance."""
        self._model = model

    @activity.defn
    async def invoke_model_activity(self, input: ActivityModelInput) -> ModelOutput:
        """Activity that invokes a LangChain model with the given input.

        Args:
            input: The model input containing messages, tools, and configuration

        Returns:
            ModelOutput containing the model's response

        Raises:
            ValueError: If no model is configured
        """
        if self._model is None:
            raise ValueError("No model configured. Call set_model() first.")

        # Import LangChain components
        try:
            from langchain_core.messages import (
                AIMessage,
                HumanMessage,
                SystemMessage,
                ToolMessage,
            )
            from langchain_core.tools import StructuredTool
        except ImportError as e:
            raise ImportError(
                "LangChain is required for this activity. "
                "Install it with: pip install langchain-core"
            ) from e

        # Convert message dicts to LangChain message objects
        messages = []
        for msg_dict in input["messages"]:
            msg_type = msg_dict.get("type", "human")
            content = msg_dict.get("content", "")

            if msg_type == "human":
                messages.append(HumanMessage(content=content))
            elif msg_type == "ai":
                # Preserve tool_calls information for AI messages
                tool_calls = msg_dict.get("tool_calls", [])
                if tool_calls:
                    messages.append(AIMessage(content=content, tool_calls=tool_calls))
                else:
                    messages.append(AIMessage(content=content))
            elif msg_type == "system":
                messages.append(SystemMessage(content=content))
            elif msg_type == "tool":
                messages.append(
                    ToolMessage(
                        content=content, tool_call_id=msg_dict.get("tool_call_id", "")
                    )
                )
            else:
                # Default to human message
                messages.append(HumanMessage(content=content))

        # Convert tools if provided
        tools = []
        if "tools" in input and input["tools"]:
            for tool_input in input["tools"]:
                # Create a structured tool from the input
                def tool_func(**kwargs):
                    # This is a placeholder - actual tool execution happens in workflow
                    return f"Tool {tool_input.name} called with {kwargs}"

                tool = StructuredTool.from_function(
                    func=tool_func,
                    name=tool_input.name,
                    description=tool_input.description,
                    args_schema=tool_input.args_schema,
                )
                tools.append(tool)

        # Configure model with tools if provided
        if tools:
            model_with_tools = self._model.bind_tools(tools)
        else:
            model_with_tools = self._model

        # Apply additional model configuration
        if "temperature" in input:
            model_with_tools = model_with_tools.bind(temperature=input["temperature"])
        if "max_tokens" in input:
            model_with_tools = model_with_tools.bind(max_tokens=input["max_tokens"])
        if "model_kwargs" in input:
            model_with_tools = model_with_tools.bind(**input["model_kwargs"])

        # Invoke the model
        response = await model_with_tools.ainvoke(messages)

        # Extract tool calls if present
        tool_calls = None
        if hasattr(response, "tool_calls") and response.tool_calls:
            tool_calls = [
                {
                    "name": tool_call.get("name"),
                    "args": tool_call.get("args", {}),
                    "id": tool_call.get("id"),
                }
                for tool_call in response.tool_calls
            ]

        # Extract usage information if available
        usage = None
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            usage = {
                "input_tokens": response.usage_metadata.get("input_tokens"),
                "output_tokens": response.usage_metadata.get("output_tokens"),
                "total_tokens": response.usage_metadata.get("total_tokens"),
            }

        return ModelOutput(content=response.content, tool_calls=tool_calls, usage=usage)
