# Standalone Activities Implementation - Remaining Work

This document tracks the remaining work for the Python SDK implementation of Standalone Activities.

Reference: See `cross-sdk-design.md` for the full cross-SDK design specification.

---

## Status Overview

| Category | Status |
|----------|--------|
| Core client methods (`start_activity`, `execute_activity`, `list_activities`, `count_activities`, `get_activity_handle`) | ✅ Complete |
| `ActivityHandle` with `result()`, `describe()`, `cancel()`, `terminate()` | ✅ Complete |
| Type-safe overloads (matching workflow activities) | ✅ Complete |
| `ActivityExecution` and `ActivityExecutionDescription` dataclasses | ⚠️ Missing fields |
| Interceptor support | ✅ Complete |
| `activity.Info` changes | ✅ Complete |
| `ActivitySerializationContext` changes | ✅ Complete |
| Basic tests | ✅ Complete |
| Type checking tests | ✅ Complete |

---

## Must Complete (Missing from Spec)

### 1. `get_activity_handle()` Implementation
- **Status:** ✅ Complete
- **Location:** `temporalio/client.py`
- **Description:** Returns a handle to an existing standalone activity by ID, allowing callers to get results, describe, cancel, or terminate an activity they didn't start.
- **Test:** `tests/test_activity.py::test_get_activity_handle`

### 2. Missing Field: `ActivityExecution.state_transition_count`
- **Status:** ❌ Not implemented
- **Location:** `temporalio/client.py` - `ActivityExecution` dataclass
- **Spec:** `state_transition_count: Optional[int]` - not always present on List operation, see proto docs
- **Effort:** Low

### 3. Missing Field: `ActivityExecutionDescription.eager_execution_requested`
- **Status:** ❌ Not implemented
- **Location:** `temporalio/client.py` - `ActivityExecutionDescription` dataclass
- **Spec:** `eager_execution_requested: bool`
- **Effort:** Low

### 4. Missing Field: `ActivityExecutionDescription.paused`
- **Status:** ❌ Not implemented
- **Location:** `temporalio/client.py` - `ActivityExecutionDescription` dataclass
- **Spec:** `paused: bool`
- **Effort:** Low

### 5. Type Fix: `ActivityExecutionCountAggregationGroup.group_values`
- **Status:** ❌ Incorrect type
- **Location:** `temporalio/client.py` - `ActivityExecutionCountAggregationGroup` dataclass
- **Current:** `Sequence[Any]`
- **Spec:** `Sequence[temporalio.common.SearchAttributeValue]`
- **Effort:** Low

---

## Intentionally Deferred (Significant Refactoring Required)

### 1. `GetActivityResultInput` and `get_activity_result` Interceptor Method
- **Status:** 🔄 Deferred
- **Description:** The current implementation caches the result directly in `ActivityHandle._known_outcome` and doesn't expose a separate interceptor point for getting results. Adding this would require refactoring the result caching logic.
- **Decision:** Intentional - not blocking release

### 2. `ActivityExecutionDescription` Does Not Extend `ActivityExecution`
- **Status:** 🔄 Deferred
- **Description:** Python frozen dataclasses don't support inheritance well. The two classes are separate with duplicated fields.
- **Decision:** Stylistic difference, doesn't affect functionality

### 3. Typed Overload Methods
- **Status:** 🔄 Deferred
- **Description:** The following methods are not implemented:
  - `start_activity_class`
  - `start_activity_method`
  - `execute_activity_class`
  - `execute_activity_method`
- **Decision:** Optional per spec - provides better type inference for class-based and method-based activity definitions

---

## Spec Documentation Needed

These items are implemented but not documented in the spec. The spec should be updated to include them.

### 1. `PendingActivityState` Enum
- **Location:** `temporalio/common.py`
- **Description:** Added to support `ActivityExecutionDescription.run_state`
- **Action:** Add to Python section of `cross-sdk-design.md`

### 2. `ActivityFailedError` Exception Class
- **Location:** `temporalio/client.py`
- **Description:** New error class for standalone activity failures, distinct from workflow `ActivityError` which has required history event fields
- **Action:** Add to Python section of `cross-sdk-design.md`

### 3. `ActivityExecutionDescription.input` Field
- **Location:** `temporalio/client.py`
- **Description:** Extra field providing deserialized activity input. Useful for debugging.
- **Action:** Decide if spec should include this or if it's Python-specific

---

## Minor Type Differences to Review

| Field | Spec | Implementation | Notes |
|-------|------|----------------|-------|
| `ActivityHandle.activity_run_id` | `Optional[str]` | ✅ `str \| None` | Fixed - now matches spec |
| `ActivityExecutionDescription.retry_policy` | `Optional` | Not optional | Should verify proto field optionality |

---

## Test Coverage

### Existing Tests (`tests/test_activity.py`)
- ✅ `test_describe` - Describe a running activity
- ✅ `test_get_result` - Get result after activity completes
- ✅ `test_get_activity_handle` - Get handle by ID, with/without run_id and result_type
- ✅ `test_manual_completion` - Complete activity manually via async handle
- ✅ `test_manual_cancellation` - Cancel activity then report cancellation via async handle
- ✅ `test_manual_failure` - Fail activity manually via async handle
- ✅ `test_manual_heartbeat` - Heartbeat from async handle

### Additional Tests Needed

#### Functional Tests
- [ ] Test `list_activities()` with various queries
- [ ] Test `count_activities()` with various queries
- [ ] Test activity ID reuse policies
- [ ] Test activity ID conflict policies
- [ ] Test search attributes on activities
- [ ] Test priority on activities
- [ ] Test retry policy behavior
- [ ] Test cancellation flow (worker-side)
- [ ] Test termination flow

#### Overload/API Variation Tests
Tests for different ways to call `start_activity`/`execute_activity`:
- ✅ Activity by callable (typed): `client.start_activity(my_activity, args=[arg1, arg2], ...)`
- ✅ Activity by name (string): `client.start_activity("my_activity", args=[arg1], result_type=MyResult, ...)`
- ✅ Single arg as positional: `client.start_activity(my_activity, arg1, ...)`
- ✅ Async activity function (`async def my_activity`)
- ✅ Sync activity function (`def my_activity`)
- ✅ With explicit `result_type` parameter
- ✅ Without `result_type` (inferred from callable)

#### Type Checking Tests (using `tests/test_type_errors.py` machinery)

✅ **DONE** - Created `tests/test_activity_type_errors.py`

**Working type checks:**
- Infers `ActivityHandle[ReturnType]` from typed async/sync activity callables
- Catches wrong type assignments for `handle.result()` and `execute_activity()` results
- Catches missing required parameters (`id`, `task_queue`)
- Catches `ActivityHandle` type parameter mismatches
- Catches wrong argument types with type-safe single-param overloads

**Overloads implemented** (matching workflow activity overloads):
1. `CallableAsyncNoParam[ReturnType]` - async, no params
2. `CallableSyncNoParam[ReturnType]` - sync, no params
3. `CallableAsyncSingleParam[ParamType, ReturnType]` with `arg: ParamType` - async, typed single param
4. `CallableSyncSingleParam[ParamType, ReturnType]` with `arg: ParamType` - sync, typed single param
5. `Callable[..., Awaitable[ReturnType]]` with `args: Sequence[Any]` - async, multi-param
6. `Callable[..., ReturnType]` with `args: Sequence[Any]` - sync, multi-param
7. `str` with `arg: Any`, `args: Sequence[Any]` - string name

---

## Notes

### Breaking Change: `activity.Info` Fields Now Optional
The following fields in `activity.Info` are now `str | None` instead of `str`:
- `workflow_id`
- `workflow_namespace` (deprecated, use `namespace`)
- `workflow_run_id`
- `workflow_type`

For standalone activities, these will be `None`. Code accessing these fields should check for `None` or use the new `in_workflow` property.

### New Field: `activity.Info.namespace`
A new non-optional `namespace` field has been added that is always set, regardless of whether the activity is standalone or workflow-triggered.

### Deprecation: `activity.Info.workflow_namespace`
This field is deprecated in favor of `namespace`. Both fields have the same value when set.

