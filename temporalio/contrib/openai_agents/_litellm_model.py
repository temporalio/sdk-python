"""LiteLLM model class used when models are prefixed with 'litellm/'.
The routing logic is handled in the ModelActivity itself.
"""

from typing import Optional, Union

from agents import Model, ModelResponse, Usage
from agents.models.chatcmpl_converter import Converter
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails
from openai.types.chat import ChatCompletionMessage

try:
    import litellm
    from litellm import acompletion
    LITELLM_AVAILABLE = True
except ImportError:
    LITELLM_AVAILABLE = False
    litellm = None
    acompletion = None


class LiteLLMConverter:
    """Helper class to convert LiteLLM messages to OpenAI format."""
    
    @classmethod
    def convert_message_to_openai(cls, message) -> ChatCompletionMessage:
        """Convert LiteLLM message to OpenAI ChatCompletionMessage format."""
        if hasattr(message, 'role') and message.role != "assistant":
            raise ValueError(f"Unsupported role: {message.role}")

        # Convert tool calls if present
        tool_calls = None
        if hasattr(message, 'tool_calls') and message.tool_calls:
            tool_calls = []
            for tc in message.tool_calls:
                tool_calls.append({
                    'id': tc.id,
                    'type': tc.type,
                    'function': {
                        'name': tc.function.name,
                        'arguments': tc.function.arguments
                    }
                })

        # Handle provider-specific fields like refusal
        provider_specific_fields = getattr(message, 'provider_specific_fields', None) or {}
        refusal = provider_specific_fields.get('refusal', None)

        return ChatCompletionMessage(
            content=getattr(message, 'content', None),
            refusal=refusal,
            role="assistant",
            tool_calls=tool_calls,
        )


class LiteLLMModel(Model):
    """LiteLLM model implementation compatible with OpenAI Agents SDK interface.
    
    This model provides the same interface as other models in the OpenAI Agents SDK
    while using LiteLLM's unified API to communicate with various LLM providers.
    """
    
    def __init__(
        self,
        model: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        **kwargs
    ):
        """Initialize the LiteLLM model.
        
        Args:
            model: Model name/identifier (e.g., "gpt-4", "anthropic/claude-3-5-sonnet")
            api_key: API key for the model provider
            base_url: Optional base URL for custom endpoints
            **kwargs: Additional configuration for LiteLLM
        """
        if not LITELLM_AVAILABLE:
            raise ImportError(
                "LiteLLM is not installed. Install it with: pip install litellm"
            )
            
        self._model = model
        self._api_key = api_key
        self._base_url = base_url
        self._config = kwargs
            
    async def get_response(
        self,
        system_instructions: Union[str, None],
        input: Union[str, list],
        model_settings,
        tools: list,
        output_schema=None,
        handoffs: list = None,
        tracing=None,
        *,
        previous_response_id: Union[str, None] = None,
        prompt=None,
        **kwargs
    ):
        """Get a response from the LiteLLM model.
        
        This method translates OpenAI Agents SDK parameters to LiteLLM's
        completion API format and returns a ModelResponse.
        """
        
        # Build messages from system instructions and input
        messages = []
        
        if system_instructions:
            messages.append({"role": "system", "content": system_instructions})
            
        # Handle different input types
        if isinstance(input, str):
            messages.append({"role": "user", "content": input})
        elif isinstance(input, list):
            # Handle list of messages/content items
            for item in input:
                if isinstance(item, dict):
                    messages.append(item)
                else:
                    # Convert other types to user message
                    messages.append({"role": "user", "content": str(item)})
        
        # Prepare LiteLLM parameters
        litellm_params = {
            "model": self._model,
            "messages": messages,
        }
        
        # Add API key if available
        if self._api_key:
            litellm_params["api_key"] = self._api_key
            
        # Add base URL if available  
        if self._base_url:
            litellm_params["base_url"] = self._base_url
            
        # Map model settings to LiteLLM parameters
        if hasattr(model_settings, 'temperature') and model_settings.temperature is not None:
            litellm_params["temperature"] = model_settings.temperature
        if hasattr(model_settings, 'max_tokens') and model_settings.max_tokens is not None:
            litellm_params["max_tokens"] = model_settings.max_tokens
        if hasattr(model_settings, 'top_p') and model_settings.top_p is not None:
            litellm_params["top_p"] = model_settings.top_p
            
        # Handle tools if provided
        if tools:
            # Convert tools to OpenAI format that LiteLLM expects
            litellm_tools = []
            for tool in tools:
                if hasattr(tool, 'name') and hasattr(tool, 'description'):
                    tool_def = {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description,
                        }
                    }
                    if hasattr(tool, 'params_json_schema'):
                        tool_def["function"]["parameters"] = tool.params_json_schema
                    litellm_tools.append(tool_def)
            
            if litellm_tools:
                litellm_params["tools"] = litellm_tools
                
        # Handle output schema/response format
        if output_schema and not (hasattr(output_schema, 'is_plain_text') and output_schema.is_plain_text()):
            if hasattr(output_schema, 'json_schema'):
                try:
                    schema = output_schema.json_schema()
                    litellm_params["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {
                            "name": getattr(output_schema, 'name', lambda: "response")(),
                            "schema": schema,
                            "strict": getattr(output_schema, 'is_strict_json_schema', lambda: False)()
                        }
                    }
                except Exception:
                    # Fallback to basic JSON mode if schema extraction fails
                    litellm_params["response_format"] = {"type": "json_object"}
        
        # Add any additional config
        litellm_params.update(self._config)
        
        try:
            # Make the LiteLLM API call
            response = await acompletion(**litellm_params)
            
            # Convert response to ModelResponse format using the same approach as official openai-agents
            choice = response.choices[0]
            message = choice.message
            
            # Convert LiteLLM message to OpenAI ChatCompletionMessage format
            openai_message = LiteLLMConverter.convert_message_to_openai(message)
            
            # Convert message to output items using agents SDK converter
            output_items = Converter.message_to_output_items(openai_message)
            
            # Handle usage conversion similar to official implementation
            if hasattr(response, "usage") and response.usage:
                response_usage = response.usage
                usage = Usage(
                    requests=1,
                    input_tokens=getattr(response_usage, 'prompt_tokens', 0),
                    output_tokens=getattr(response_usage, 'completion_tokens', 0), 
                    total_tokens=getattr(response_usage, 'total_tokens', 0),
                    input_tokens_details=InputTokensDetails(
                        cached_tokens=getattr(
                            getattr(response_usage, 'prompt_tokens_details', None), 
                            'cached_tokens', 0
                        ) or 0
                    ),
                    output_tokens_details=OutputTokensDetails(
                        reasoning_tokens=getattr(
                            getattr(response_usage, 'completion_tokens_details', None), 
                            'reasoning_tokens', 0
                        ) or 0
                    )
                )
            else:
                # Fallback if no usage data
                usage = Usage(
                    requests=1,
                    input_tokens=0,
                    output_tokens=0,
                    total_tokens=0,
                    input_tokens_details=InputTokensDetails(cached_tokens=0),
                    output_tokens_details=OutputTokensDetails(reasoning_tokens=0)
                )
            
            # Create ModelResponse
            model_response = ModelResponse(
                output=output_items,
                usage=usage,
                response_id=getattr(response, 'id', None)
            )
            
            return model_response
            
        except Exception as e:
            # Re-raise with more context
            raise RuntimeError(f"LiteLLM API call failed for model {self._model}: {str(e)}") from e
    
    def stream_response(
        self,
        system_instructions: Optional[str],
        input: Union[str, list],
        model_settings,
        tools: list,
        output_schema=None,
        handoffs: list = None,
        tracing=None,
        *,
        previous_response_id: Optional[str] = None,
        prompt=None,
        **kwargs
    ):
        """Stream response from the LiteLLM model.
        
        Note: Streaming implementation would require additional complexity
        to handle the OpenAI Agents SDK streaming interface. This is a 
        placeholder for future implementation.
        """
        raise NotImplementedError(
            "Streaming is not yet implemented for LiteLLM models. "
            "Use get_response() for non-streaming responses."
        )