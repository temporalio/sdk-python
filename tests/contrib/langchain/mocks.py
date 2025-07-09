from dataclasses import dataclass
from typing import Dict, List, Optional
from temporalio import activity
from langchain_core.language_models import BaseLanguageModel


# Simple mock objects that don't inherit from LangChain (avoids sandbox issues)
@dataclass
class MockModelResponse:
    content: str
    tool_calls: Optional[List[Dict]] = None
    usage_metadata: Optional[Dict] = None
    response_metadata: Optional[Dict] = None


class SimpleMockModel:
    """Simple mock model that doesn't inherit from LangChain."""

    def __init__(self, response_text: str = "Mock response"):
        self.model_name = "simple-mock-model"
        self.response_text = response_text

    async def ainvoke(self, input_data, **kwargs) -> MockModelResponse:
        return MockModelResponse(
            f"Mock response to '{input_data}': {self.response_text}"
        )

    def invoke(self, input_data, **kwargs) -> MockModelResponse:
        return MockModelResponse(
            f"Sync mock response to '{input_data}': {self.response_text}"
        )


class SimpleMockTool:
    """Simple mock tool that doesn't inherit from LangChain."""

    def __init__(self, name: str = "simple_tool", response: str = "Tool result"):
        self.name = name
        self.description = f"A simple mock tool named {name}"
        self.response = response

    async def ainvoke(self, input_data, **kwargs) -> str:
        return f"Async tool {self.name} result for '{input_data}': {self.response}"

    def invoke(self, input_data, **kwargs) -> str:
        return f"Sync tool {self.name} result for '{input_data}': {self.response}"


class DummyChatOpenAI(BaseLanguageModel):
    """Minimal BaseLanguageModel subclass to satisfy wrapper validation."""

    def __init__(self, model: str = "gpt-4o") -> None:  # noqa: D401
        super().__init__()
        object.__setattr__(self, "model_name", model)

    def model_dump(self):
        """Pydantic serialization method."""
        return {"model_name": self.model_name}

    @classmethod
    def model_validate(cls, data):
        """Pydantic deserialization method."""
        return cls(model=data.get("model_name", "gpt-4o"))

    async def ainvoke(self, messages, **kwargs):  # type: ignore
        if not activity.in_activity():
            raise RuntimeError("Not in activity context")
        return "dummy-response"

    def invoke(self, messages, **kwargs):  # type: ignore
        if not activity.in_activity():
            raise RuntimeError("Not in activity context")
        return "dummy-response-sync"

    # Required abstract methods
    def _llm_type(self) -> str:  # noqa: D401
        return "dummy"

    def generate_prompt(self, prompts, stop=None, **kwargs):  # noqa: D401
        if not activity.in_activity():
            raise RuntimeError("Not in activity context")
        return "prompt"

    async def agenerate_prompt(self, prompts, stop=None, **kwargs):  # noqa: D401
        if not activity.in_activity():
            raise RuntimeError("Not in activity context")
        return "prompt"

    def predict(self, text, **kwargs):  # noqa: D401
        if not activity.in_activity():
            raise RuntimeError("Not in activity context")
        return "prediction"

    async def apredict(self, text, **kwargs):  # noqa: D401
        if not activity.in_activity():
            raise RuntimeError("Not in activity context")
        return "async prediction"

    def predict_messages(self, messages, **kwargs):  # noqa: D401
        if not activity.in_activity():
            raise RuntimeError("Not in activity context")
        return "msg"

    async def apredict_messages(self, messages, **kwargs):  # noqa: D401
        if not activity.in_activity():
            raise RuntimeError("Not in activity context")
        return "amsg"
