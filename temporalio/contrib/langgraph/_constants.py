"""Constants used throughout the LangGraph Temporal integration."""

# Node names
START_NODE = "__start__"
"""The special start node that begins graph execution."""

TOOLS_NODE = "tools"
"""The standard name for the tools node in LangGraph agents."""

# Output keys used in graph execution results
INTERRUPT_KEY = "__interrupt__"
"""Key in output dict indicating an interrupt occurred."""

CHECKPOINT_KEY = "__checkpoint__"
"""Key in output dict containing checkpoint data for continue-as-new."""

# Channel prefixes
BRANCH_PREFIX = "branch:"
"""Prefix for branch channel names in conditional edges."""

# Model node names - common names for nodes that invoke LLMs
MODEL_NODE_NAMES = frozenset({"agent", "model", "llm", "chatbot", "chat_model"})
"""Common names for model/LLM nodes in LangGraph graphs."""

# Attributes to check when extracting model name from node metadata
MODEL_NAME_ATTRS = ("model_name", "model")
"""Attribute names to check when extracting model name from objects."""
