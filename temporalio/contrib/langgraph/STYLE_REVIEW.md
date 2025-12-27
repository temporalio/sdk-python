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
| 3 | ~~Data structures~~ | ~~Low~~ | ~~Uses Pydantic instead of dataclasses~~ **FIXED** |
| 4 | ~~Docstrings~~ | ~~Low~~ | ~~Different style from SDK conventions~~ **FIXED** |
| 5 | ~~Logging~~ | ~~Medium~~ | ~~No module-level logger defined~~ **FIXED** |
| 6 | ~~Warnings suppression~~ | ~~Medium~~ | ~~Suppresses deprecation warnings~~ **FIXED** |
| 7 | File organization | Low | Example file in production code |
| 8 | Test naming | Low | Uses `e2e_` prefix not standard in SDK |
| 9 | ~~Type annotations~~ | ~~Low~~ | ~~Mixed `Optional[X]` and `X | None`~~ **FIXED** |
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

### 3. Pydantic Models vs Dataclasses **FIXED**

**Severity**: Low
**Location**: `_models.py`

**Issue**: The SDK predominantly uses `@dataclass` (often `@dataclass(frozen=True)`) for data structures, while the LangGraph integration was using Pydantic `BaseModel`.

**Resolution**: Converted all models in `_models.py` from Pydantic `BaseModel` to Python `@dataclass`:
- Replaced `BaseModel` inheritance with `@dataclass` decorator
- Replaced `model_config = ConfigDict(arbitrary_types_allowed=True)` (no longer needed for dataclasses)
- Replaced Pydantic's `BeforeValidator` for `LangGraphState` with `__post_init__` method in `NodeActivityInput`
- Updated to SDK-style inline docstrings after field definitions
- Converted `Optional[X]` to `X | None` for consistency

The models now follow SDK conventions while maintaining full functionality:
```python
@dataclass
class StoreItem:
    """A key-value pair within a namespace."""

    namespace: tuple[str, ...]
    """Hierarchical namespace tuple."""

    key: str
    """The key within the namespace."""

    value: dict[str, Any]
    """The stored value."""
```

Note: `_coerce_to_message()` still uses Pydantic's `TypeAdapter` internally for LangChain message deserialization, which is acceptable since LangChain already depends on Pydantic.

---

### 4. Docstring Style Inconsistencies **FIXED**

**Severity**: Low
**Location**: Various files

**Issue**: Original concern was about module docstrings and attribute documentation style.

**Resolution**: The module now follows SDK conventions:

#### 4a. Module Docstrings
All module docstrings use short, single-sentence style:
- `_activities.py`: "Temporal activities for LangGraph node execution."
- `_models.py`: "Dataclass models for LangGraph-Temporal integration."
- `_plugin.py`: "LangGraph plugin for Temporal integration."
- etc.

The `__init__.py` includes an experimental warning which is appropriate for a public API.

#### 4b. Attribute Documentation
All dataclasses in `_models.py` use SDK-style inline docstrings after attributes:
```python
@dataclass
class StoreItem:
    """A key-value pair within a namespace."""

    namespace: tuple[str, ...]
    """Hierarchical namespace tuple."""

    key: str
    """The key within the namespace."""
```

This pattern was established when converting from Pydantic to dataclasses (item #3).

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

### 9. Type Annotations Style **FIXED**

**Severity**: Low
**Location**: Various files

**Issue**: Mixed use of `Optional[X]` and `X | None`.

**Resolution**: Standardized all type annotations to use `X | None` syntax throughout the module:
- `_temporal_tool.py` - Converted all `Optional` usages
- `_runner.py` - Converted all `Optional` usages
- `_model_registry.py` - Removed unused `Optional` import
- `_temporal_model.py` - Converted all `Optional` usages
- `__init__.py` - Converted all `Optional` usages in public APIs
- `_store.py` - Converted all `Optional` usages

All files now consistently use the `X | None` syntax preferred by newer SDK code.

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
- [x] Convert Pydantic models to dataclasses (item #3) **FIXED** - Converted all models in `_models.py` to dataclasses
- [ ] Move example file (item #7)
- [x] Standardize type annotation style (item #9) **FIXED** - Converted all `Optional[X]` to `X | None` syntax
- [ ] Move design document (item #11)
- [x] Align docstring style (item #4) **FIXED** - Module and attribute docstrings follow SDK conventions
- [ ] Review test organization (item #8)
