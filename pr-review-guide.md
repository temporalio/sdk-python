# PR Review Guide: Client-Side Activity API

This document provides links comparing the new client-side activity API with the existing workflow-side activity API.

**Repository:** https://github.com/temporalio/sdk-python
**Branch:** `standalone-activity`

---

## Important Context

**This is not a greenfield design.** The differences documented here fall into several categories:

1. **Fundamentally different** - Genuinely different requirements (e.g., RPC vs workflow commands)
2. **Incrementally different** - Different today but likely to converge as CHASM unifies activity execution
3. **Constrained by history** - Different because changing the existing workflow API would break users
4. **Open questions** - Differences that may need resolution as the system evolves

### Confirmed from Design Documents (July 2025)

- **Separate ID spaces**: Standalone activities have their **own ID space**, separate from workflows. A workflow and a standalone activity CAN have the same ID simultaneously. This is intentional to avoid cross-contamination in list views and confusing error messages.
- **Visibility for workflow activities**: Explicitly on roadmap as "Post MLP" item. Will come after standalone activities ship.
- **CHASM unification**: The current workflow activity implementation will be **deprecated** and not receive new features. All activities will eventually run on CHASM.
- **Memos NOT supported**: Unlike workflows, standalone activities do not support memos (at least not in MLP).
- **Pause/Reset/UpdateOptions**: Planned for MLP GA (not pre-release). Standalone activities will support pausing, resetting, and updating options at runtime.
- **Run ID**: Standalone activities have a system-generated `run_id` (like workflow run IDs), enabling activity ID reuse after completion.
- **CLI unification**: `temporal activity` commands will work for both standalone AND workflow activities, differentiated by `--activity-id` vs `--workflow-id` flags.

### Future Roadmap (Post-MLP)

- **Starting standalone activities from workflows**: `workflow.startStandaloneActivity()` and `workflow.getStandaloneActivityHandle()` are planned
- **Eager execution**: Return first task inline in start response for latency optimization
- **Completion callbacks**: Server-side webhooks when activity reaches terminal status
- **Activity as scheduled action**: Schedule activities directly without wrapper workflow

---

## 1. Start/Execute Activity (Functions)

Start an activity by passing an activity function reference. Returns a handle (`start_activity`) or awaits result (`execute_activity`).

| Workflow Implementation | Client Implementation |
|------------------------|----------------------|
| [temporalio/workflow.py (`start_activity`)](https://github.com/temporalio/sdk-python/blob/main/temporalio/workflow.py#L2267) | [temporalio/client.py (`Client.start_activity`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L1435) |
| [temporalio/workflow.py (`execute_activity`)](https://github.com/temporalio/sdk-python/blob/main/temporalio/workflow.py#L2483) | [temporalio/client.py (`Client.execute_activity`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L1677) |
| [tests/worker/test_workflow.py (`SimpleActivityWorkflow`)](https://github.com/temporalio/sdk-python/blob/main/tests/worker/test_workflow.py#L815) | [tests/test_activity.py (`test_get_result`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/tests/test_activity.py#L363) |

### Difference Analysis

| Parameter | Workflow | Client | Category |
|-----------|----------|--------|----------|
| `id` / `activity_id` | Optional (`activity_id: str \| None = None`), auto-generates `"1"`, `"2"`, ... from sequence | **Required** (`id: str`) | **Constrained by history**: Workflow API has always auto-generated IDs. Changing to required would break existing code. Standalone has no history to generate from. **Design confirmed**: Standalone activities have a **separate ID space** from workflows (a workflow and standalone activity CAN share the same ID). |
| `task_queue` | Optional (defaults to workflow's task queue) | **Required** | **Fundamentally different**: Workflows have an inherent task queue; standalone activities don't. |
| `id_reuse_policy` | Not present | Present with default `ALLOW_DUPLICATE` | **Open question**: Will workflow activities eventually support this for consistency? Currently, workflow activities get unique IDs per workflow execution. |
| `id_conflict_policy` | Not present | Present with default `FAIL` | **Open question**: Same as above - may be needed if/when workflow activity IDs become more explicit. |
| `search_attributes` | Not present | Present | **Incrementally different**: Workflow activities will likely gain visibility/search attributes as CHASM unifies the model. |
| `cancellation_type` | Present | Not present | **Fundamentally different**: Workflow cancellation semantics (TRY_CANCEL, WAIT_CANCELLATION_COMPLETED, ABANDON) are about deterministic replay behavior. Standalone activities have different cancellation semantics. |
| `versioning_intent` | Present (deprecated) | Not present | **Fundamentally different**: Worker Versioning is workflow-specific (deprecated anyway). |
| `rpc_metadata` / `rpc_timeout` | Not present | Present | **Fundamentally different**: Client calls are direct RPC; workflow scheduling is through commands. |

---

## 2. Start/Execute Activity (Callable Classes)

Start an activity defined as a callable class (with `__call__` method). Pass the class type; the worker registers an instance.

| Workflow Implementation | Client Implementation |
|------------------------|----------------------|
| [temporalio/workflow.py (`start_activity_class`)](https://github.com/temporalio/sdk-python/blob/main/temporalio/workflow.py#L2643) | [temporalio/client.py (`Client.start_activity_class`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L1880) |
| [temporalio/workflow.py (`execute_activity_class`)](https://github.com/temporalio/sdk-python/blob/main/temporalio/workflow.py#L2800) | [temporalio/client.py (`Client.execute_activity_class`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L2072) |
| [tests/worker/test_workflow.py (`test_workflow_activity_callable_class`)](https://github.com/temporalio/sdk-python/blob/main/tests/worker/test_workflow.py#L3017) | [tests/test_activity.py (`test_start_activity_class_async`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/tests/test_activity.py#L935) |

### Difference Analysis

Same differences as Section 1 (Start/Execute Activity Functions). The `_class` variants mirror the function variants with identical parameter differences.

---

## 3. Start/Execute Activity (Methods)

Start an activity defined as a method on a class. Pass an unbound method reference; the worker registers bound methods from an instance.

| Workflow Implementation | Client Implementation |
|------------------------|----------------------|
| [temporalio/workflow.py (`start_activity_method`)](https://github.com/temporalio/sdk-python/blob/main/temporalio/workflow.py#L2957) | [temporalio/client.py (`Client.start_activity_method`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L2219) |
| [temporalio/workflow.py (`execute_activity_method`)](https://github.com/temporalio/sdk-python/blob/main/temporalio/workflow.py#L3114) | [temporalio/client.py (`Client.execute_activity_method`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L2366) |
| [tests/worker/test_workflow.py (`test_workflow_activity_method`)](https://github.com/temporalio/sdk-python/blob/main/tests/worker/test_workflow.py#L3067) | [tests/test_activity.py (`test_start_activity_method_async`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/tests/test_activity.py#L1027) |

### Difference Analysis

Same differences as Section 1 (Start/Execute Activity Functions). The `_method` variants mirror the function variants with identical parameter differences.

---

## 4. Activity Handle

Handle to an activity execution for awaiting result, cancelling, describing, etc.

| Workflow Implementation | Client Implementation |
|------------------------|----------------------|
| [temporalio/workflow.py (`ActivityHandle`)](https://github.com/temporalio/sdk-python/blob/main/temporalio/workflow.py#L2085) | [temporalio/client.py (`ActivityHandle`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L4555) |
| — | [temporalio/client.py (`ActivityHandle.result`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L4618) |
| — | [temporalio/client.py (`ActivityHandle.cancel`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L4702) |
| — | [temporalio/client.py (`ActivityHandle.terminate`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L4738) |
| — | [temporalio/client.py (`ActivityHandle.describe`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L4769) |
| — | [temporalio/client.py (`Client.get_activity_handle`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L2506) |

### Difference Analysis

| Aspect | Workflow | Client | Category |
|--------|----------|--------|----------|
| Base class | Extends `asyncio.Task` (awaitable directly) | Generic class with explicit `result()` method | **Fundamentally different**: Workflow activities integrate with the deterministic event loop. Client activities poll via RPC. |
| Result retrieval | `await handle` | `await handle.result()` | **Constrained by history**: Workflow API is established. Different execution models make unification difficult. |
| `activity_run_id` property | Not present | Present | **Incrementally different**: Workflow activities may gain run IDs as CHASM unifies. |
| `cancel()` method | Simple `cancel()` | Rich `cancel(reason, wait_for_cancel_completed, ...)` | **Fundamentally different** (sort of): Workflow cancellation must be immediate for replay. But `reason` could potentially be added to workflow cancel. |
| `terminate()` method | Not present | Present | **Open question**: Could workflow-started activities be terminated externally via client? This is more about what operations are available where. |
| `describe()` method | Not present | Present | **Incrementally different**: Describe for workflow activities would make sense once they're in visibility. |
| Result caching | N/A (event loop managed) | Explicit `_cached_result` / `_result_fetched` | **Fundamentally different**: Different execution models. |

---

## 5. List/Count Activities

Query activity executions by visibility query.

| Workflow Implementation | Client Implementation |
|------------------------|----------------------|
| N/A (client-only feature) | [temporalio/client.py (`Client.list_activities`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L2416) |
| — | [temporalio/client.py (`Client.count_activities`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L2462) |
| — | [temporalio/client.py (`ActivityExecutionAsyncIterator`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L3991) |
| — | [temporalio/client.py (`ActivityExecution`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L4099) |
| — | [temporalio/client.py (`ActivityExecutionCount`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L4207) |
| — | [tests/test_activity.py (`test_list_activities`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/tests/test_activity.py#L430) |
| — | [tests/test_activity.py (`test_count_activities`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/tests/test_activity.py#L455) |

### Difference Analysis

**Incrementally different**: Currently client-only because standalone activities are being added to visibility first.

⚠️ **Future consideration**: Visibility queries for workflow-started activities are a natural evolution. When this happens:
- Will the same `list_activities()` / `count_activities()` APIs return both?
- Will there be a way to filter by "started by workflow" vs "standalone"?
- How will activity ID collisions between workflow activities (auto-generated `"1"`, `"2"`) and standalone activities (user-provided) be handled in query results?

These APIs mirror `Client.list_workflows()` / `Client.count_workflows()` patterns.

---

## 6. Describe Activity

Get detailed information about an activity execution.

| Workflow Implementation | Client Implementation |
|------------------------|----------------------|
| N/A (client-only feature) | [temporalio/client.py (`ActivityHandle.describe`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L4769) |
| — | [temporalio/client.py (`ActivityExecutionDescription`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L4241) |
| — | [tests/test_activity.py (`test_describe`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/tests/test_activity.py#L79) |

### Difference Analysis

**Incrementally different**: Currently client-only, but describing workflow-started activities would make sense once they're in visibility.

⚠️ **Future consideration**: Will `ActivityExecutionDescription` need additional fields to indicate whether the activity was started by a workflow vs standalone?

---

## 7. Cancel Activity

Request cancellation of an activity execution.

| Workflow Implementation | Client Implementation |
|------------------------|----------------------|
| [temporalio/workflow.py (`ActivityHandle.cancel`)](https://github.com/temporalio/sdk-python/blob/main/temporalio/workflow.py#L2107) | [temporalio/client.py (`ActivityHandle.cancel`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L4702) |
| — | [tests/test_activity.py (`test_manual_cancellation`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/tests/test_activity.py#L561) |

### Difference Analysis

| Aspect | Workflow | Client | Category |
|--------|----------|--------|----------|
| Method signature | `cancel()` (no parameters) | `cancel(reason, wait_for_cancel_completed, rpc_metadata, rpc_timeout)` | Mixed |
| `reason` parameter | Not present | Present | **Could potentially align**: A reason could be useful for workflow cancellation too. |
| `wait_for_cancel_completed` | Not present | Present | **Fundamentally different**: Workflows cannot block for determinism. |
| RPC options | Not present | `rpc_metadata`, `rpc_timeout` | **Fundamentally different**: Client calls are direct RPC. |

---

## 8. Terminate Activity

Forcefully terminate an activity execution (client-only, no workflow equivalent).

| Workflow Implementation | Client Implementation |
|------------------------|----------------------|
| N/A (client-only feature) | [temporalio/client.py (`ActivityHandle.terminate`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L4738) |
| — | [tests/test_activity.py (`test_terminate`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/tests/test_activity.py#L898) |

### Difference Analysis

**Open question**: Is this truly "client-only" or just "not available from within workflow code"?

Consider: Could a client terminate a workflow-started activity? The current design assumes no, but this could be an administrative operation like `WorkflowHandle.terminate()`.

This mirrors `WorkflowHandle.terminate()`.

---

## 9. Async Activity Completion (Manual Completion)

Complete/fail/heartbeat an activity asynchronously from outside the activity execution context.

| Workflow Implementation | Client Implementation |
|------------------------|----------------------|
| [temporalio/client.py (`AsyncActivityHandle`)](https://github.com/temporalio/sdk-python/blob/main/temporalio/client.py#L5355) | Same (existing API) |
| [temporalio/client.py (`Client.get_async_activity_handle`)](https://github.com/temporalio/sdk-python/blob/main/temporalio/client.py#L2552) | Same (existing API) |
| — | [tests/test_activity.py (`test_manual_completion`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/tests/test_activity.py#L524) |
| — | [tests/test_activity.py (`test_manual_failure`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/tests/test_activity.py#L612) |
| — | [tests/test_activity.py (`test_manual_heartbeat`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/tests/test_activity.py#L680) |

### Difference Analysis

**Same API**: The existing `AsyncActivityHandle` for async completion works for both workflow-started and standalone activities. This is one area where the APIs are already unified because async completion is inherently a client-side operation working with task tokens.

---

## 10. Interceptors

Intercept client-side activity operations.

| Workflow Implementation | Client Implementation |
|------------------------|----------------------|
| [temporalio/worker/_interceptor.py (`StartActivityInput`)](https://github.com/temporalio/sdk-python/blob/main/temporalio/worker/_interceptor.py#L247) | [temporalio/client.py (`StartActivityInput`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L7395) |
| [temporalio/worker/_interceptor.py (`WorkflowOutboundInterceptor.start_activity`)](https://github.com/temporalio/sdk-python/blob/main/temporalio/worker/_interceptor.py#L453) | [temporalio/client.py (`OutboundInterceptor.start_activity`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L7847) |
| — | [temporalio/client.py (`GetActivityResultInput`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L7468) |
| — | [temporalio/client.py (`OutboundInterceptor.get_activity_result`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L7881) |
| — | [temporalio/client.py (`CancelActivityInput`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L7423) |
| — | [temporalio/client.py (`OutboundInterceptor.cancel_activity`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L7855) |
| — | [temporalio/client.py (`TerminateActivityInput`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L7439) |
| — | [temporalio/client.py (`OutboundInterceptor.terminate_activity`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L7863) |
| — | [temporalio/client.py (`DescribeActivityInput`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L7454) |
| — | [temporalio/client.py (`OutboundInterceptor.describe_activity`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L7871) |
| — | [temporalio/client.py (`ListActivitiesInput`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L7483) |
| — | [temporalio/client.py (`OutboundInterceptor.list_activities`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L7891) |
| — | [temporalio/client.py (`CountActivitiesInput`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L7499) |
| — | [temporalio/client.py (`OutboundInterceptor.count_activities`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/client.py#L7901) |
| — | [tests/test_activity.py (`ActivityTracingInterceptor`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/tests/test_activity.py#L100) |
| — | [tests/test_activity.py (`test_start_activity_calls_interceptor`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/tests/test_activity.py#L161) |

### Difference Analysis

**`StartActivityInput` differences:**

| Field | Workflow Interceptor | Client Interceptor | Category |
|-------|---------------------|-------------------|----------|
| `activity` vs `activity_type` | `activity: str` | `activity_type: str` | ⚠️ **Naming inconsistency**: Should align. |
| `activity_id` vs `id` | `activity_id: str \| None` | `id: str` (required) | **Constrained by history**: Can't change workflow API. |
| `task_queue` | `str \| None` | `str` (required) | **Fundamentally different**: Different defaults. |
| `cancellation_type` | Present | Not present | **Fundamentally different**: Workflow-specific. |
| `disable_eager_execution` | Present | Not present | **Fundamentally different**: Workflow-specific optimization. |
| `versioning_intent` | Present | Not present | **Fundamentally different**: Workflow versioning specific. |
| `id_reuse_policy` | Not present | Present | **Open question**: May need to align as CHASM unifies. |
| `id_conflict_policy` | Not present | Present | **Open question**: Same as above. |
| `search_attributes` | Not present | Present | **Incrementally different**: Workflow activities may gain this. |
| `rpc_metadata` / `rpc_timeout` | Not present | Present | **Fundamentally different**: Client calls are direct RPC. |
| `arg_types` / `ret_type` | Present | Not present | ⚠️ **Review**: Consider adding for type consistency. |

**Additional client-side interceptors**: These are new operations that may eventually apply to workflow-started activities too:
- `GetActivityResultInput` / `get_activity_result` - For polling results
- `CancelActivityInput` / `cancel_activity` - For cancellation
- `TerminateActivityInput` / `terminate_activity` - For termination
- `DescribeActivityInput` / `describe_activity` - For describe
- `ListActivitiesInput` / `list_activities` - For listing
- `CountActivitiesInput` / `count_activities` - For counting

---

## 11. Activity Info

Information available within a running activity. Updated to support activities not started by a workflow.

| Workflow Implementation | Client Implementation |
|------------------------|----------------------|
| [temporalio/activity.py (`Info`)](https://github.com/temporalio/sdk-python/blob/main/temporalio/activity.py#L74) | Same class, updated docstrings |
| [temporalio/activity.py (`Info.workflow_id`)](https://github.com/temporalio/sdk-python/blob/main/temporalio/activity.py#L108) | Now `None` if activity not started by workflow |
| [temporalio/activity.py (`Info.workflow_run_id`)](https://github.com/temporalio/sdk-python/blob/main/temporalio/activity.py#L114) | Now `None` if activity not started by workflow |
| [temporalio/activity.py (`Info.workflow_type`)](https://github.com/temporalio/sdk-python/blob/main/temporalio/activity.py#L117) | Now `None` if activity not started by workflow |
| [temporalio/activity.py (`Info.activity_run_id`)](https://github.com/temporalio/sdk-python/blob/main/temporalio/activity.py#L98) | Set for activities not started by workflow |
| [temporalio/activity.py (`Info.in_workflow`)](https://github.com/temporalio/sdk-python/blob/main/temporalio/activity.py#L145) | Property to check if started by workflow |

### Difference Analysis

| Field | Before | After | Category |
|-------|--------|-------|----------|
| `workflow_id` | `str` (always set) | `str \| None` | **Necessary change**: Standalone activities don't have a parent workflow. |
| `workflow_run_id` | `str` (always set) | `str \| None` | **Necessary change**: Same reason. |
| `workflow_type` | `str` (always set) | `str \| None` | **Necessary change**: Same reason. |
| `activity_run_id` | Not present | `str \| None = None` | **Design confirmed**: Standalone activities have system-generated run IDs (like workflow run IDs), enabling activity ID reuse. None for workflow activities; may be added post-CHASM. |
| `in_workflow` property | Not present | Added | **Pragmatic addition**: Convenience for the breaking type change. |

⚠️ **Breaking change note**: Existing code that assumes `workflow_id` is always set will need to handle `None`. The `in_workflow` property provides a clean way to check.

---

## 12. Common Enums

Enums for activity ID policies and execution status.

| Workflow Implementation | Client Implementation |
|------------------------|----------------------|
| N/A (client-only) | [temporalio/common.py (`ActivityIDReusePolicy`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/common.py) |
| — | [temporalio/common.py (`ActivityIDConflictPolicy`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/common.py) |
| — | [temporalio/common.py (`ActivityExecutionStatus`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/temporalio/common.py) |
| — | [tests/test_activity.py (`test_id_conflict_policy_fail`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/tests/test_activity.py#L721) |
| — | [tests/test_activity.py (`test_id_reuse_policy_reject_duplicate`)](https://github.com/temporalio/sdk-python/blob/standalone-activity/tests/test_activity.py#L774) |

### Difference Analysis

**Currently standalone-only**, mirroring workflow equivalents:
- `ActivityIDReusePolicy` - Mirrors `WorkflowIDReusePolicy`
- `ActivityIDConflictPolicy` - Mirrors `WorkflowIDConflictPolicy`
- `ActivityExecutionStatus` - Mirrors `WorkflowExecutionStatus`

⚠️ **Open question**: As CHASM unifies and workflow activities potentially get explicit IDs (or share visibility), will these policies apply to both?

---

## 13. Type Tests

Static type checking tests for overload type inference.

| Workflow Implementation | Client Implementation |
|------------------------|----------------------|
| — | [tests/test_activity_type_errors.py](https://github.com/temporalio/sdk-python/blob/standalone-activity/tests/test_activity_type_errors.py) |

### Difference Analysis

**New test file**: Tests pyright type inference for the new client-side activity API overloads.

---

## Summary

### Fundamentally Different (unlikely to converge)

1. **RPC vs commands**: Client calls are direct RPC with `rpc_metadata`/`rpc_timeout`; workflow scheduling is through deterministic commands.
2. **Cancellation semantics**: `cancellation_type` (TRY_CANCEL, WAIT_CANCELLATION_COMPLETED, ABANDON) is about workflow replay behavior; standalone cancellation is different.
3. **Handle as asyncio.Task**: Workflow handles extend `asyncio.Task` for deterministic event loop integration; client handles cannot.
4. **`wait_for_cancel_completed`**: Workflows cannot block waiting for cancellation; clients can.

### Constrained by History (can't easily change)

1. **`activity_id` optional vs `id` required**: Workflow API has always auto-generated IDs (`"1"`, `"2"`, ...). Can't make required without breaking users.
2. **`task_queue` optional vs required**: Workflow API defaults to workflow's task queue. Can't remove default.
3. **Field naming**: `activity` vs `activity_type`, `activity_id` vs `id` in interceptors.

### Incrementally Different (confirmed to converge as CHASM unifies)

1. **Visibility operations**: `list_activities()`, `count_activities()`, `describe()` are currently client-only. **Confirmed for Post-MLP**: Will work for workflow activities in the future.
2. **Search attributes**: Currently standalone-only; **confirmed**: workflow activities will gain visibility post-CHASM.
3. **`activity_run_id`**: Standalone activities have run IDs; workflow activities currently don't. May be added post-CHASM.
4. **ID policies**: `id_reuse_policy` and `id_conflict_policy` may become relevant for workflow activities post-CHASM.

### Resolved Questions (from Design Docs)

1. ✅ **Activity ID namespace**: Standalone activities have a **separate ID space** from workflows. A workflow and standalone activity can have the same ID.
2. ✅ **Visibility for workflow activities**: Confirmed for Post-MLP roadmap.
3. ✅ **CHASM unification**: Confirmed. Current workflow activity impl will be deprecated.
4. ✅ **Cancellation types**: Confirmed NOT applicable to standalone. Different model with cancel/terminate operations.

### Remaining Open Questions

1. **Workflow activity vs standalone activity ID collision**: If a workflow starts an activity with ID "foo" and someone starts a standalone activity with ID "foo", do these collide? Design docs clarify workflows vs activities are separate, but what about workflow-*activities* vs standalone activities?
2. **Cross-boundary operations**: Can a client terminate/describe a workflow-started activity once visibility exists?
3. **Policy convergence**: Will `id_reuse_policy` / `id_conflict_policy` apply to workflow activities post-CHASM?
4. **Visibility scope**: When workflow activities gain visibility, will `list_activities()` return both? How to filter?

### Items to Address in This PR

1. ⚠️ **Naming**: `activity` vs `activity_type` in interceptor inputs - should align where possible.
2. ⚠️ **Type info**: Workflow interceptor has `arg_types`/`ret_type` but client doesn't - consider adding for consistency.
3. ⚠️ **Breaking change**: `activity.Info.workflow_id` can now be `None` - ensure migration docs are clear.
