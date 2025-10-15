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
    """Builders for creating model responses for testing."""

    @staticmethod
    def model_response(output: TResponseOutputItem) -> ModelResponse:
        """Create a ModelResponse with the given output."""
        return ModelResponse(
            output=[output],
            usage=Usage(),
            response_id=None,
        )

    @staticmethod
    def response_output_message(text: str) -> ResponseOutputMessage:
        """Create a ResponseOutputMessage with text content."""
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
        """Create a ModelResponse with a function tool call."""
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
        """Create a ModelResponse with an output message."""
        return ResponseBuilders.model_response(
            ResponseBuilders.response_output_message(text)
        )


class TestModelProvider(ModelProvider):
    """Test model provider which simply returns the given module."""

    __test__ = False

    def __init__(self, model: Model):
        """Initialize a test model provider with a model."""
        self._model = model

    def get_model(self, model_name: Union[str, None]) -> Model:
        """Get a model from the model provider."""
        return self._model


class TestModel(Model):
    """Test model for use mocking model responses."""

    __test__ = False

    def __init__(self, fn: Callable[[], ModelResponse]) -> None:
        """Initialize a test model with a callable."""
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
        """Get a response from the model."""
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


class StaticTestModel(TestModel):
    """Static test model for use mocking model responses.
    Set a responses attribute to a list of model responses, which will be returned sequentially.
    """

    __test__ = False
    responses: list[ModelResponse] = []

    def __init__(
        self,
    ) -> None:
        """Initialize the static test model with predefined responses."""
        self._responses = iter(self.responses)
        super().__init__(lambda: next(self._responses))
