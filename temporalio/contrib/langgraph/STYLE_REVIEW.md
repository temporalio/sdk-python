# Style and Convention Review: LangGraph Integration

This document captures discrepancies between the LangGraph integration (`temporalio/contrib/langgraph`) and the conventions used in the rest of the `sdk-python` codebase.

**Review Date**: 2025-12-26
**Reviewed Against**: sdk-python main codebase, `temporalio/contrib/openai_agents` as reference

---

## Summary Table

| # | Category | Severity | Description |
|---|----------|----------|-------------|
| 1 | ~~Experimental warnings~~ | ~~Medium~~ | ~~Missing `.. warning::` notices for experimental API~~ **FIXED** |
| 2 | ~~Internal API usage~~ | ~~High~~ | ~~Uses `langgraph._internal.*` private modules~~ **DOCUMENTED** |
| 3 | Data structures | Low | Uses Pydantic instead of dataclasses |
| 4 | Docstrings | Low | Different style from SDK conventions |
| 5 | ~~Logging~~ | ~~Medium~~ | ~~No module-level logger defined~~ **FIXED** |
| 6 | ~~Warnings suppression~~ | ~~Medium~~ | ~~Suppresses deprecation warnings~~ **FIXED** |
| 7 | File organization | Low | Example file in production code |
| 8 | Test naming | Low | Uses `e2e_` prefix not standard in SDK |
| 9 | Type annotations | Low | Mixed `Optional[X]` and `X | None` |
| 10 | ~~Exceptions~~ | ~~Medium~~ | ~~Uses generic exceptions instead of domain-specific~~ **FIXED** |
| 11 | Design docs | Low | Design document in production directory |

---

## Detailed Findings

### 1. Missing Experimental/Warning Notices

**Severity**: Medium
**Location**: All files in `temporalio/contrib/langgraph/`

**Issue**: The `openai_agents` contrib module uses RST `.. warning::` directives to mark experimental APIs:

```python
# openai_agents pattern (__init__.py, _temporal_openai_agents.py):
"""Support for using the OpenAI Agents SDK...

.. warning::
    This module is experimental and may change in future versions.
    Use with caution in production environments.
"""
```

**LangGraph Status**: No such warnings exist in the LangGraph integration's module docstrings or public API docstrings.

**Recommendation**: Add experimental warnings to:
- `__init__.py` module docstring
- `LangGraphPlugin` class docstring
- Key public functions like `compile()`, `temporal_tool()`, `temporal_model()`

---

### 2. Reliance on LangGraph Internal APIs

**Severity**: High
**Location**: `_activities.py:41-48`

**Issue**: The code imports from `langgraph._internal._constants` and `langgraph._internal._scratchpad`:

```python
from langgraph._internal._constants import (
    CONFIG_KEY_CHECKPOINT_NS,
    CONFIG_KEY_READ,
    CONFIG_KEY_RUNTIME,
    CONFIG_KEY_SCRATCHPAD,
)
from langgraph._internal._scratchpad import PregelScratchpad
```

**Risk**: These are private LangGraph APIs (prefixed with `_internal`) that may change without notice in any LangGraph release.

**Recommendation**:
- Document this dependency risk in the module
- Pin LangGraph version tightly in optional dependencies
- Consider feature request to LangGraph to expose these as public APIs
- Add integration tests that will catch breaking changes early

---

### 3. Pydantic Models vs Dataclasses

**Severity**: Low
**Location**: `_models.py`

**Issue**: The SDK predominantly uses `@dataclass` (often `@dataclass(frozen=True)`) for data structures, while the LangGraph integration uses Pydantic `BaseModel`:

```python
# SDK pattern (common.py, activity.py, etc.):
@dataclass(frozen=True)
class RetryPolicy:
    initial_interval: timedelta = timedelta(seconds=1)
    """Backoff interval for the first retry. Default 1s."""

# LangGraph pattern (_models.py):
class StoreItem(BaseModel):
    """Single item in the store."""
    namespace: tuple[str, ...]
    key: str
    value: dict[str, Any]
```

**Context**: This may be intentional due to LangChain's Pydantic dependency and serialization requirements, but creates inconsistency with the rest of the SDK.

**Recommendation**: Document why Pydantic is used (likely for LangChain compatibility) in the module docstring.

---

### 4. Docstring Style Inconsistencies

**Severity**: Low
**Location**: Various files

#### 4a. Module Docstrings

**SDK Pattern**: Short, single-sentence module docstrings:
```python
"""Activity worker."""
"""Common Temporal exceptions."""
"""Client for accessing Temporal."""
```

**LangGraph Pattern**: Longer, more detailed module docstrings with usage examples:
```python
"""Temporal integration for LangGraph.

This module provides seamless integration between LangGraph and Temporal,
enabling durable execution of LangGraph agents...

Quick Start:
    >>> from temporalio.client import Client
    ...
"""
```

#### 4b. Attribute Documentation

**SDK Pattern**: Uses inline docstrings after attributes in dataclasses:
```python
@dataclass
class RetryPolicy:
    initial_interval: timedelta = timedelta(seconds=1)
    """Backoff interval for the first retry. Default 1s."""
```

**LangGraph Pattern**: Uses `Attributes:` section in class docstring:
```python
class StoreItem(BaseModel):
    """Single item in the store.

    Attributes:
        namespace: Hierarchical namespace tuple...
        key: The key within the namespace.
        value: The stored value...
    """
```

**Recommendation**: Consider aligning with SDK's inline docstring pattern where possible.

---

### 5. No Logger Definition

**Severity**: Medium
**Location**: All files in `temporalio/contrib/langgraph/`

**Issue**: Many SDK modules define a module-level logger:
```python
logger = logging.getLogger(__name__)
```

**Found in SDK**: `_activity.py`, `_workflow.py`, `service.py`, `_worker.py`, `_replayer.py`, `_tuning.py`, etc.

**LangGraph Status**: No module-level logger is defined in any LangGraph file, even in `_activities.py` and `_runner.py` which perform complex operations.

**Recommendation**: Add loggers to:
- `_activities.py` - for activity execution logging
- `_runner.py` - for graph execution flow
- `_plugin.py` - for plugin initialization

---

### 6. Suppressed Deprecation Warnings **FIXED**

**Severity**: Medium
**Location**: `_activities.py`

**Issue**: The code was suppressing deprecation warnings when importing from LangGraph.

**Resolution**: Fixed by importing `CONFIG_KEY_SEND` and `Send` directly from `langgraph._internal._constants` and `langgraph.types` respectively at module level, avoiding the deprecated `langgraph.constants` module entirely. This removes all warning suppression code.

---

### 7. Example File in Production Code

**Severity**: Low
**Location**: `temporalio/contrib/langgraph/example.py`

**Issue**: There's an `example.py` file (451 lines) in the production module directory.

**SDK Convention**: Examples belong in:
- `tests/` directory
- Documentation
- Separate `examples/` directory at repo root

**Reference**: The `openai_agents` contrib doesn't have an example file in its module directory.

**Recommendation**: Move `example.py` to `tests/contrib/langgraph/` or a top-level `examples/` directory.

---

### 8. Test Organization Pattern

**Severity**: Low
**Location**: `tests/contrib/langgraph/`

**Current Structure**:
```
tests/contrib/langgraph/
├── e2e_graphs.py      # Graph definitions
├── e2e_workflows.py   # Workflow definitions
├── test_e2e.py        # E2E tests
├── test_*.py          # Unit tests
└── conftest.py        # Fixtures
```

**Observations**:
- The `e2e_` prefix naming is non-standard for the SDK
- SDK typically uses `conftest.py` for shared fixtures
- Helper modules usually go in `tests/helpers/`

**Recommendation**: Consider renaming `e2e_graphs.py` and `e2e_workflows.py` to remove the prefix or move to a helpers location.

---

### 9. Type Annotations Style

**Severity**: Low
**Location**: Various files

**Issue**: Mixed use of `Optional[X]` and `X | None`:

```python
# Mixed in _runner.py:
checkpoint: Optional[dict[str, Any]] = None
resume_value: Optional[Any] = None

# vs newer style:
config: dict[str, Any] | None = None
```

**SDK Trend**: Newer SDK code tends to prefer `X | None` syntax consistently.

**Recommendation**: Standardize on `X | None` syntax throughout.

---

### 10. Exception Handling Conventions **FIXED**

**Severity**: Medium
**Location**: `_exceptions.py`, `_graph_registry.py`, `_tool_registry.py`, `_model_registry.py`, `_activities.py`

**Issue**: Registry modules raised generic `ValueError` and `KeyError`.

**Resolution**: Created `_exceptions.py` module with two categories of exceptions:

1. **Activity-Level Exceptions** (cross workflow/activity boundary): Use `ApplicationError` with specific `type` constants for proper Temporal error handling:
   - `graph_not_found_error()` → `ApplicationError` with `type=GRAPH_NOT_FOUND_ERROR`
   - `node_not_found_error()` → `ApplicationError` with `type=NODE_NOT_FOUND_ERROR`
   - `tool_not_found_error()` → `ApplicationError` with `type=TOOL_NOT_FOUND_ERROR`
   - `model_not_found_error()` → `ApplicationError` with `type=MODEL_NOT_FOUND_ERROR`
   - All include relevant details via `ApplicationError.details` and are marked `non_retryable=True`

2. **Configuration Exceptions** (do not cross boundaries): Use custom exception classes inheriting from `ValueError`:
   - `GraphAlreadyRegisteredError`
   - `ToolAlreadyRegisteredError`
   - `ModelAlreadyRegisteredError`

Error type constants and exception classes are exported from `__init__.py` for user access.

---

### 11. Design Document in Production Code

**Severity**: Low
**Location**: `temporalio/contrib/langgraph/langgraph-plugin-design.md`

**Issue**: A 1400+ line design document exists in the production module directory.

**SDK Convention**: Design documents belong in:
- `docs/` directory
- GitHub wiki
- Separate design docs repository
- Or removed before release (kept in PR history)

**Recommendation**: Move to `docs/contrib/` or remove from production code.

---

## Additional Observations

### Positive Patterns

The LangGraph integration does follow several SDK conventions correctly:

1. **File naming**: Uses `_` prefix for internal modules (`_plugin.py`, `_runner.py`, etc.)
2. **`__init__.py` exports**: Properly exposes public API through `__all__`
3. **Type hints**: Comprehensive type annotations throughout
4. **`from __future__ import annotations`**: Consistently used
5. **Plugin architecture**: Follows the `SimplePlugin` pattern from `temporalio.plugin`

### Dependencies

The integration introduces dependencies on:
- `langgraph` (required)
- `langchain-core` (transitive)
- `pydantic` (transitive via langchain)

These should be documented as optional dependencies in `pyproject.toml`.

---

## Action Items

### High Priority
- [x] Address internal API usage (item #2) **DOCUMENTED** - Added detailed explanation in _activities.py
- [x] Add experimental warnings (item #1) **DONE**
- [x] Add logging infrastructure (item #5) **DONE** - Added to _activities.py, _plugin.py, _runner.py

### Medium Priority
- [x] Review warning suppression approach (item #6) **FIXED** - Removed warning suppression by importing directly from `_internal`
- [x] Consider domain-specific exceptions (item #10) **FIXED** - Created `_exceptions.py` with `ApplicationError` factory functions and configuration exceptions

### Low Priority
- [ ] Move example file (item #7)
- [ ] Standardize type annotation style (item #9)
- [ ] Move design document (item #11)
- [ ] Align docstring style (item #4)
- [ ] Review test organization (item #8)
