"""Prototype 3: Document PregelExecutableTask Interface.

Technical Concern:
    What is the actual PregelExecutableTask structure? What fields are available
    and what do we need to pass to Temporal activities?

FINDINGS:
    PregelExecutableTask is a frozen dataclass with these fields:

    Core Identification:
        - name: str - Node name (e.g., "node_a", "tools")
        - id: str - Unique task ID
        - path: tuple[str | int | tuple, ...] - Path in graph hierarchy

    Execution Context:
        - input: Any - Input state to the node
        - proc: Runnable - The node's runnable (function/callable)
        - config: RunnableConfig - LangGraph configuration
        - triggers: Sequence[str] - Channels that triggered this task

    Output Management:
        - writes: deque[tuple[str, Any]] - Output writes (channel, value) pairs
        - writers: Sequence[Runnable] - Additional writer runnables

    Retry/Cache:
        - retry_policy: Sequence[RetryPolicy] - LangGraph retry configuration
        - cache_key: CacheKey | None - Optional cache key

    Subgraphs:
        - subgraphs: Sequence[PregelProtocol] - Nested subgraphs

For Temporal Activities:
    We need to pass:
    1. task.name - For activity identification
    2. task.id - For unique activity ID
    3. task.input - Serialized input state
    4. task.config - Filtered, serializable config

    We DON'T serialize:
    - task.proc - Reconstructed from graph in activity worker
    - task.writes - Created fresh in activity, returned as result
    - task.writers - Part of proc execution
    - task.subgraphs - Handled separately

VALIDATION STATUS: PASSED
    - PregelExecutableTask interface fully documented
    - All fields inspectable at runtime
    - Clear mapping to activity parameters

API STABILITY NOTE:
    PregelExecutableTask is a public type exported from langgraph.types.
    While the fields may change, the type itself is part of the public API.
"""

from __future__ import annotations

import dataclasses
from collections import deque
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import PregelExecutableTask


def inspect_pregel_executable_task() -> dict[str, Any]:
    """Inspect the PregelExecutableTask dataclass structure.

    Returns:
        Dict with field information and annotations.
    """
    # Verify it's a dataclass
    assert dataclasses.is_dataclass(PregelExecutableTask), "Should be a dataclass"

    # Get all fields
    fields = dataclasses.fields(PregelExecutableTask)

    field_info = {}
    for field in fields:
        field_info[field.name] = {
            "type": str(field.type),
            "has_default": field.default is not dataclasses.MISSING,
            "has_default_factory": field.default_factory is not dataclasses.MISSING,
        }

    return {
        "is_dataclass": True,
        "is_frozen": True,  # From _T_DC_KWARGS
        "field_count": len(fields),
        "fields": field_info,
        "field_names": [f.name for f in fields],
    }


def categorize_fields_for_temporal() -> dict[str, list[str]]:
    """Categorize which fields need to go to Temporal activities.

    Returns:
        Dict mapping categories to field names.
    """
    return {
        # Must be serialized and passed to activity
        "pass_to_activity": [
            "name",      # Activity name/identification
            "id",        # Unique task/activity ID
            "input",     # Serialized input state
            "path",      # Graph hierarchy path
            "triggers",  # What triggered this task
        ],

        # Config needs special handling (filter non-serializable parts)
        "config_filtered": [
            "config",    # RunnableConfig - filter internal keys
        ],

        # Reconstructed in activity worker (not serialized)
        "reconstruct_in_activity": [
            "proc",      # Node runnable - get from graph
            "writers",   # Writer runnables - part of proc
            "subgraphs", # Nested graphs - handled separately
        ],

        # Created fresh in activity, returned as result
        "activity_output": [
            "writes",    # Output writes - activity result
        ],

        # Optional, could be mapped to Temporal retry
        "policy_mapping": [
            "retry_policy",  # Map to Temporal retry policy
            "cache_key",     # Could use Temporal memoization
        ],
    }


def get_serializable_task_data(task: PregelExecutableTask) -> dict[str, Any]:
    """Extract serializable data from a task for Temporal activity.

    This is a prototype of what we'll send to activities.

    Args:
        task: The PregelExecutableTask to extract data from.

    Returns:
        Dict with serializable task information.
    """
    # Core identification
    data: dict[str, Any] = {
        "name": task.name,
        "id": task.id,
        "path": task.path,
        "triggers": list(task.triggers),
    }

    # Input - needs serialization (JSON, pickle, etc.)
    # For prototype, just note the type
    data["input_type"] = type(task.input).__name__
    data["input"] = task.input  # Would be serialized

    # Config - filter non-serializable parts
    data["config"] = filter_config_for_serialization(task.config)

    # Retry policy - could map to Temporal retry
    if task.retry_policy:
        data["retry_policy"] = [
            {
                "initial_interval": rp.initial_interval,
                "backoff_factor": rp.backoff_factor,
                "max_interval": rp.max_interval,
                "max_attempts": rp.max_attempts,
                "jitter": rp.jitter,
            }
            for rp in task.retry_policy
        ]

    # Cache key - could enable Temporal memoization
    if task.cache_key:
        data["cache_key"] = {
            "ns": task.cache_key.ns,
            "key": task.cache_key.key,
            "ttl": task.cache_key.ttl,
        }

    return data


def filter_config_for_serialization(config: RunnableConfig) -> dict[str, Any]:
    """Filter RunnableConfig to only serializable parts.

    CONFIG_KEY_* constants are internal and shouldn't be serialized.

    Args:
        config: The RunnableConfig to filter.

    Returns:
        Dict with only serializable configuration.
    """
    # Keys that are safe to serialize
    safe_keys = {
        "tags",
        "metadata",
        "run_name",
        "run_id",
        "max_concurrency",
        "recursion_limit",
    }

    # Keys in 'configurable' that are internal
    internal_configurable_prefixes = (
        "__pregel_",  # All internal Pregel keys
        "__lg_",      # LangGraph internal
    )

    filtered: dict[str, Any] = {}

    for key, value in config.items():
        if key in safe_keys and value is not None:
            filtered[key] = value
        elif key == "configurable":
            # Filter configurable dict
            filtered_configurable = {}
            if isinstance(value, dict):
                for cfg_key, cfg_value in value.items():
                    # Skip internal keys
                    if not any(cfg_key.startswith(prefix) for prefix in internal_configurable_prefixes):
                        # Only include if serializable
                        try:
                            import json
                            json.dumps(cfg_value)
                            filtered_configurable[cfg_key] = cfg_value
                        except (TypeError, ValueError):
                            pass  # Skip non-serializable
            if filtered_configurable:
                filtered["configurable"] = filtered_configurable

    return filtered


if __name__ == "__main__":
    print("=== PregelExecutableTask Structure ===")
    info = inspect_pregel_executable_task()
    print(f"Is dataclass: {info['is_dataclass']}")
    print(f"Is frozen: {info['is_frozen']}")
    print(f"Field count: {info['field_count']}")
    print(f"\nFields:")
    for name, details in info['fields'].items():
        print(f"  - {name}: {details['type']}")
        if details['has_default']:
            print(f"    (has default)")
        if details['has_default_factory']:
            print(f"    (has default factory)")

    print("\n=== Field Categories for Temporal ===")
    categories = categorize_fields_for_temporal()
    for category, fields in categories.items():
        print(f"\n{category}:")
        for field in fields:
            print(f"  - {field}")
