"""Testing utilities for OpenAI agents."""

from collections.abc import AsyncIterator, Callable, Sequence
from typing import Optional, Union

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

from temporalio.client import Client
from temporalio.contrib.openai_agents._mcp import (
    StatefulMCPServerProvider,
    StatelessMCPServerProvider,
)
from temporalio.contrib.openai_agents._model_parameters import ModelActivityParameters
from temporalio.contrib.openai_agents._temporal_openai_agents import OpenAIAgentsPlugin

__all__ = [
    "AgentEnvironment",
    "ResponseBuilders",
    "TestModel",
    "TestModelProvider",
]


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

    def get_model(self, model_name: str | None) -> Model:
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
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        **kwargs,
    ) -> ModelResponse:
        """Get a response from the mocked model, by calling the callable passed to the constructor."""
        return self.fn()

    def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
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


class AgentEnvironment:
    """Testing environment for OpenAI agents with Temporal integration.

    This async context manager provides a convenient way to set up testing environments
    for OpenAI agents with mocked model calls and Temporal integration.

    .. warning::
        This API is experimental and may change in the future.

    Example:
        >>> from temporalio.contrib.openai_agents.testing import AgentEnvironment, TestModelProvider, ResponseBuilders
        >>> from temporalio.client import Client
        >>>
        >>> # Create a mock model that returns predefined responses
        >>> mock_model = TestModel.returning_responses([
        ...     ResponseBuilders.output_message("Hello, world!"),
        ...     ResponseBuilders.output_message("How can I help you?")
        ... ])
        >>>
        >>> async with AgentEnvironment(model=mock_model) as env:
        ...     client = env.applied_on_client(client)
        ...     # Use client for testing workflows with mocked model calls
    """

    __test__ = False

    def __init__(
        self,
        model_params: ModelActivityParameters | None = None,
        model_provider: ModelProvider | None = None,
        model: Model | None = None,
        mcp_server_providers: Sequence[
            StatelessMCPServerProvider | StatefulMCPServerProvider
        ] = (),
        register_activities: bool = True,
    ) -> None:
        """Initialize the AgentEnvironment.

        Args:
            model_params: Configuration parameters for Temporal activity execution
                of model calls. If None, default parameters will be used.
            model_provider: Optional model provider for custom model implementations.
                Only one of model_provider or model should be provided.
                If both are provided, model_provider will be used.
            model: Optional model for custom model implementations.
                Use TestModel for mocking model responses.
                Equivalent to model_provider=TestModelProvider(model).
                Only one of model_provider or model should be provided.
                If both are provided, model_provider will be used.
            mcp_server_providers: Sequence of MCP servers to automatically register with the worker.
            register_activities: Whether to register activities during worker execution.

        .. warning::
           This API is experimental and may change in the future.
        """
        self._model_params = model_params
        self._model_provider = None
        if model_provider is not None:
            self._model_provider = model_provider
        elif model is not None:
            self._model_provider = TestModelProvider(model)
        self._mcp_server_providers = mcp_server_providers
        self._register_activities = register_activities
        self._plugin: OpenAIAgentsPlugin | None = None

    async def __aenter__(self) -> "AgentEnvironment":
        """Enter the async context manager."""
        # Create the plugin with the provided configuration
        self._plugin = OpenAIAgentsPlugin(
            model_params=self._model_params,
            model_provider=self._model_provider,
            mcp_server_providers=self._mcp_server_providers,
            register_activities=self._register_activities,
        )

        return self

    async def __aexit__(self, *args) -> None:
        """Exit the async context manager."""
        # No cleanup needed currently
        pass

    def applied_on_client(self, client: Client) -> Client:
        """Apply the agent environment's plugin to a client and return a new client instance.

        Args:
            client: The base Temporal client to apply the plugin to.

        Returns:
            A new Client instance with the OpenAI agents plugin applied.

        .. warning::
           This API is experimental and may change in the future.
        """
        if self._plugin is None:
            raise RuntimeError(
                "AgentEnvironment must be entered before applying to client"
            )

        new_config = client.config()
        existing_plugins = new_config.get("plugins", [])
        new_config["plugins"] = list(existing_plugins) + [self._plugin]
        return Client(**new_config)

    @property
    def openai_agents_plugin(self) -> OpenAIAgentsPlugin:
        """Get the underlying OpenAI agents plugin.

        .. warning::
           This API is experimental and may change in the future.
        """
        if self._plugin is None:
            raise RuntimeError(
                "AgentEnvironment must be entered before accessing plugin"
            )
        return self._plugin
