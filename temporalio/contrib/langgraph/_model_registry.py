"""Registry for LangChain chat models used in Temporal activities."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Callable

from temporalio.contrib.langgraph._exceptions import model_not_found_error

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

# Global registries
_model_instances: dict[str, "BaseChatModel"] = {}
_model_factories: dict[str, Callable[[], "BaseChatModel"]] = {}
_registry_lock = threading.Lock()


def register_model(model: "BaseChatModel", name: str | None = None) -> None:
    """Register a model instance in the global registry."""
    if name is None:
        name = getattr(model, "model_name", None) or getattr(model, "model", None)

    if name is None:
        raise ValueError(
            "Could not determine model name. Either pass a name explicitly "
            "or ensure the model has a 'model_name' or 'model' attribute."
        )

    with _registry_lock:
        _model_instances[name] = model


def register_model_factory(name: str, factory: Callable[[], "BaseChatModel"]) -> None:
    """Register a factory function for lazy model instantiation."""
    with _registry_lock:
        _model_factories[name] = factory


def get_model(name: str) -> "BaseChatModel":
    """Get a model from the registry by name."""
    with _registry_lock:
        # Check instances first
        if name in _model_instances:
            return _model_instances[name]

        # Try factories
        if name in _model_factories:
            model = _model_factories[name]()
            # Cache the instance
            _model_instances[name] = model
            return model

        # Try to auto-create common models
        auto_model = _try_auto_create_model(name)
        if auto_model is not None:
            _model_instances[name] = auto_model
            return auto_model

        available = list(set(_model_instances.keys()) | set(_model_factories.keys()))
        raise model_not_found_error(name, available)


def _try_auto_create_model(name: str) -> "BaseChatModel | None":
    """Try to auto-create a model based on common naming patterns."""
    model: "BaseChatModel | None" = None
    try:
        # OpenAI models
        if name.startswith("gpt-") or name.startswith("o1"):
            from langchain_openai import ChatOpenAI

            model = ChatOpenAI(model=name)

        # Anthropic models
        elif name.startswith("claude-"):
            from langchain_anthropic import ChatAnthropic

            model = ChatAnthropic(model=name)  # type: ignore[call-arg]

        # Google models
        elif name.startswith("gemini-"):
            from langchain_google_genai import ChatGoogleGenerativeAI

            model = ChatGoogleGenerativeAI(model=name)  # type: ignore[call-arg]

    except ImportError:
        # Required package not installed
        pass

    return model


def get_all_models() -> dict[str, "BaseChatModel"]:
    """Get all registered model instances."""
    with _registry_lock:
        return dict(_model_instances)


def clear_registry() -> None:
    """Clear all registered models. Mainly for testing."""
    with _registry_lock:
        _model_instances.clear()
        _model_factories.clear()
