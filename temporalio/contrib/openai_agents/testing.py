"""Testing utilities for OpenAI agents."""

from typing import AsyncIterator, Callable, Optional, Union

from agents import (
    AgentOutputSchemaBase,
    Handoff,
    Model,
    ModelProvider,
    ModelResponse,
    ModelSettings,
    ModelTracing,
    Tool,
    TResponseInputItem,
    Usage,
)
from agents.items import TResponseOutputItem, TResponseStreamEvent
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)


class ResponseBuilders:
    """Builders for creating model responses for testing.

    .. warning::
        This API is experimental and may change in the future.
    """

    @staticmethod
    def model_response(output: TResponseOutputItem) -> ModelResponse:
        """Create a ModelResponse with the given output.

        .. warning::
           This API is experimental and may change in the future.
        """
        return ModelResponse(
            output=[output],
            usage=Usage(),
            response_id=None,
        )

    @staticmethod
    def response_output_message(text: str) -> ResponseOutputMessage:
        """Create a ResponseOutputMessage with text content.

        .. warning::
           This API is experimental and may change in the future.
        """
        return ResponseOutputMessage(
            id="",
            content=[
                ResponseOutputText(
                    text=text,
                    annotations=[],
                    type="output_text",
                )
            ],
            role="assistant",
            status="completed",
            type="message",
        )

    @staticmethod
    def tool_call(arguments: str, name: str) -> ModelResponse:
        """Create a ModelResponse with a function tool call.

        .. warning::
           This API is experimental and may change in the future.
        """
        return ResponseBuilders.model_response(
            ResponseFunctionToolCall(
                arguments=arguments,
                call_id="call",
                name=name,
                type="function_call",
                id="id",
                status="completed",
            )
        )

    @staticmethod
    def output_message(text: str) -> ModelResponse:
        """Create a ModelResponse with an output message.

        .. warning::
           This API is experimental and may change in the future.
        """
        return ResponseBuilders.model_response(
            ResponseBuilders.response_output_message(text)
        )


class TestModelProvider(ModelProvider):
    """Test model provider which simply returns the given module.

    .. warning::
        This API is experimental and may change in the future.
    """

    __test__ = False

    def __init__(self, model: Model):
        """Initialize a test model provider with a model.

        .. warning::
           This API is experimental and may change in the future.
        """
        self._model = model

    def get_model(self, model_name: Union[str, None]) -> Model:
        """Get a model from the model provider.

        .. warning::
           This API is experimental and may change in the future.
        """
        return self._model


class TestModel(Model):
    """Test model for use mocking model responses.

    .. warning::
        This API is experimental and may change in the future.
    """

    __test__ = False

    def __init__(self, fn: Callable[[], ModelResponse]) -> None:
        """Initialize a test model with a callable.

        .. warning::
           This API is experimental and may change in the future.
        """
        self.fn = fn

    async def get_response(
        self,
        system_instructions: Union[str, None],
        input: Union[str, list[TResponseInputItem]],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: Union[AgentOutputSchemaBase, None],
        handoffs: list[Handoff],
        tracing: ModelTracing,
        **kwargs,
    ) -> ModelResponse:
        """Get a response from the mocked model, by calling the callable passed to the constructor."""
        return self.fn()

    def stream_response(
        self,
        system_instructions: Optional[str],
        input: Union[str, list[TResponseInputItem]],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: Optional[AgentOutputSchemaBase],
        handoffs: list[Handoff],
        tracing: ModelTracing,
        **kwargs,
    ) -> AsyncIterator[TResponseStreamEvent]:
        """Get a streamed response from the model. Unimplemented."""
        raise NotImplementedError()

    @staticmethod
    def returning_responses(responses: list[ModelResponse]) -> "TestModel":
        """Create a mock model which sequentially returns responses from a list.

        .. warning::
           This API is experimental and may change in the future.
        """
        i = iter(responses)
        return TestModel(lambda: next(i))
