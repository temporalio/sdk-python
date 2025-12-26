"""Registry for LangChain chat models used in Temporal activities.

This module provides a global registry for chat models that are wrapped with
temporal_model(). The registry allows the execute_chat_model activity to look
up models by name or retrieve registered instances.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

# Global registries
_model_instances: dict[str, "BaseChatModel"] = {}
_model_factories: dict[str, Callable[[], "BaseChatModel"]] = {}
_registry_lock = threading.Lock()


def register_model(model: "BaseChatModel", name: Optional[str] = None) -> None:
    """Register a model instance in the global registry.

    Args:
        model: The LangChain chat model instance to register.
        name: Optional name for the model. If not provided, uses the model's
            model_name or model attribute.

    Raises:
        ValueError: If the model name cannot be determined.
    """
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
    """Register a factory function for creating model instances.

    Use this when you want to lazily instantiate models in the activity
    rather than passing model instances through the workflow.

    Args:
        name: The model name that will trigger this factory.
        factory: A callable that returns a BaseChatModel instance.

    Example:
        >>> from langchain_openai import ChatOpenAI
        >>>
        >>> register_model_factory(
        ...     "gpt-4o",
        ...     lambda: ChatOpenAI(model="gpt-4o", temperature=0)
        ... )
        >>>
        >>> # Now temporal_model("gpt-4o") will use this factory
    """
    with _registry_lock:
        _model_factories[name] = factory


def get_model(name: str) -> "BaseChatModel":
    """Get a model from the registry by name.

    First checks for a registered instance, then tries factories.

    Args:
        name: The name of the model to retrieve.

    Returns:
        A BaseChatModel instance.

    Raises:
        KeyError: If no model with the given name is registered.
    """
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
        raise KeyError(
            f"Model '{name}' not found in registry. "
            f"Available models: {available}. "
            f"Register the model using register_model() or register_model_factory(), "
            f"or pass a model instance to temporal_model() instead of a string."
        )


def _try_auto_create_model(name: str) -> Optional["BaseChatModel"]:
    """Try to auto-create a model based on common naming patterns.

    This provides convenience for common model names without requiring
    explicit registration.

    Args:
        name: The model name.

    Returns:
        A BaseChatModel instance if auto-creation succeeded, None otherwise.
    """
    model: Optional["BaseChatModel"] = None
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
    """Get all registered model instances.

    Returns:
        A copy of the model instances dict.
    """
    with _registry_lock:
        return dict(_model_instances)


def clear_registry() -> None:
    """Clear all registered models. Mainly for testing."""
    with _registry_lock:
        _model_instances.clear()
        _model_factories.clear()
