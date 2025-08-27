# Nexus Cancellation Type Test Failure Analysis

## CI Run Information

- **GitHub Actions Run**: [Run #17270021185](https://github.com/temporalio/sdk-python/actions/runs/17270021185)
- **Analyzed Test**: `tests/nexus/test_workflow_caller_cancellation_types.py::test_cancellation_type[TRY_CANCEL]`
- **Note**: This is the regular test variant where the cancel handler successfully cancels the workflow

## Test: `test_cancellation_type[TRY_CANCEL]`

### Expected Behavior

For `TRY_CANCEL` cancellation type, the test expects:
```
caller_op_future_resolved < op_cancel_requested_event < op_cancel_request_completed_event
```

This means the caller's operation future should be resolved as cancelled BEFORE the cancel request event appears in the history.

### Test Results by Environment

**Passing Environments:**
- ✅ macOS Intel (Python 3.13)
- ✅ macOS Intel (Python 3.9)

**Failing Environments:**
- ❌ [Windows (Python 3.13)](https://github.com/temporalio/sdk-python/actions/runs/17270021185/job/49011534523)
- ❌ [Windows (Python 3.9)](https://github.com/temporalio/sdk-python/actions/runs/17270021185/job/49011535105)
- ❌ [macOS ARM (Python 3.13)](https://github.com/temporalio/sdk-python/actions/runs/17270021185/job/49011534847)
- ❌ [macOS ARM (Python 3.9)](https://github.com/temporalio/sdk-python/actions/runs/17270021185/job/49011535271)

### FAILING CASE (Windows - 3.13)

**Assertion Error:**
```
E       assert datetime.datetime(2025, 8, 27, 14, 43, 39, 500768, tzinfo=datetime.timezone.utc) < datetime.datetime(2025, 8, 27, 14, 43, 39, 498647, tzinfo=datetime.timezone.utc)
```

**Timing:**
- `op_cancel_requested_event`: 498647 ms (33ms in history)
- `caller_op_future_resolved`: 500768 ms (2121 ms AFTER!)
- This violates the expected order

**Interleaved History (Failing):**
*(Timestamps shown are milliseconds relative to workflow start)*
```
caller-wf-03883524-ebba-4792-87c1-66959c77da91  | handler-wf-dc94e9a2-7f80-4669-99ab-eacf8258dc94
----------------------------------------------------------------------------------
| 1:    0 WORKFLOW_EXECUTION_STARTED             |
| 2:    0 WORKFLOW_TASK_SCHEDULED                |
| 3:    3 WORKFLOW_TASK_STARTED                  |
| 4:   13 WORKFLOW_TASK_COMPLETED                |
| 5:   13 WORKFLOW_EXECUTION_UPDATE_ACCEPTED     |
| 6:   13 NEXUS_OPERATION_SCHEDULED              |
| 7:   23 NEXUS_OPERATION_STARTED                |  1:   23 WORKFLOW_EXECUTION_STARTED
| 8:   23 WORKFLOW_TASK_SCHEDULED                |  2:   23 WORKFLOW_TASK_SCHEDULED
| 9:   23 WORKFLOW_TASK_STARTED                  |  3:   23 WORKFLOW_TASK_STARTED
|                                                |  4:   23 WORKFLOW_TASK_COMPLETED
|10:   33 WORKFLOW_TASK_COMPLETED                |
|11:   33 NEXUS_OPERATION_CANCEL_REQUESTED       |  <-- Cancel requested at 33ms
|12:   37 WORKFLOW_EXECUTION_UPDATE_COMPLETED    |
|13:   37 SIGNAL_EXTERNAL_WORKFLOW_EXECUTION_INI |
|14:   39 EXTERNAL_WORKFLOW_EXECUTION_SIGNALED   |  5:   39 WORKFLOW_EXECUTION_SIGNALED
|15:   39 WORKFLOW_TASK_SCHEDULED                |  6:   39 WORKFLOW_TASK_SCHEDULED
|                                                |  7:   39 WORKFLOW_TASK_STARTED
|16:   44 WORKFLOW_TASK_STARTED                  |  9:   44 WORKFLOW_EXECUTION_CANCEL_REQUESTED
|17:   49 WORKFLOW_TASK_COMPLETED                |  8:   49 WORKFLOW_TASK_COMPLETED
|18:   49 NEXUS_OPERATION_CANCEL_REQUEST_COMPLET |  10:  49 WORKFLOW_TASK_SCHEDULED
|                                                |  11:  49 WORKFLOW_TASK_STARTED
|19:   54 NEXUS_OPERATION_CANCELED               |  12:  54 WORKFLOW_TASK_COMPLETED
|20:   54 WORKFLOW_TASK_SCHEDULED                |  13:  54 WORKFLOW_EXECUTION_CANCELED
|21:   64 WORKFLOW_EXECUTION_SIGNALED            |  <-- Signal sent AFTER future resolved (2+ seconds later!)
|22:   64 WORKFLOW_TASK_STARTED                  |
|23:   64 WORKFLOW_TASK_COMPLETED                |
|24:   64 WORKFLOW_EXECUTION_COMPLETED           |
```

### PASSING CASE (macOS Intel - 3.13)

**Interleaved History (Passing):**
*(Timestamps shown are milliseconds relative to workflow start)*
```
caller-wf-ca042943-ad26-4870-a94f-2cc1e8c26db6  | handler-wf-e7e9649b-dae7-477d-8730-13b2f63e45f5
----------------------------------------------------------------------------------
| 1:    0 WORKFLOW_EXECUTION_STARTED             |
| 2:    0 WORKFLOW_TASK_SCHEDULED                |
| 3:    4 WORKFLOW_TASK_STARTED                  |
| 4:   14 WORKFLOW_TASK_COMPLETED                |
| 5:   14 WORKFLOW_EXECUTION_UPDATE_ACCEPTED     |
| 6:   14 NEXUS_OPERATION_SCHEDULED              |
|                                                |  1:   25 WORKFLOW_EXECUTION_STARTED
|                                                |  2:   26 WORKFLOW_TASK_SCHEDULED
|                                                |  3:   28 WORKFLOW_TASK_STARTED
| 7:   32 NEXUS_OPERATION_STARTED                |
| 8:   32 WORKFLOW_TASK_SCHEDULED                |
| 9:   34 WORKFLOW_TASK_STARTED                  |
|                                                |  4:   35 WORKFLOW_TASK_COMPLETED
|10:   42 WORKFLOW_TASK_COMPLETED                |
|11:   42 NEXUS_OPERATION_CANCEL_REQUESTED       |  <-- Cancel requested at 42ms
|12:   42 WORKFLOW_EXECUTION_UPDATE_COMPLETED    |
|13:   42 SIGNAL_EXTERNAL_WORKFLOW_EXECUTION_INI |
|                                                |  5:   45 WORKFLOW_EXECUTION_SIGNALED
|                                                |  6:   45 WORKFLOW_TASK_SCHEDULED
|14:   46 EXTERNAL_WORKFLOW_EXECUTION_SIGNALED   |
|15:   46 WORKFLOW_TASK_SCHEDULED                |  7:   47 WORKFLOW_TASK_STARTED
|16:   49 WORKFLOW_TASK_STARTED                  |
|                                                |  9:   56 WORKFLOW_EXECUTION_CANCEL_REQUESTED
|17:   60 WORKFLOW_TASK_COMPLETED                |  8:   64 WORKFLOW_TASK_COMPLETED
|                                                |  10:  64 WORKFLOW_TASK_SCHEDULED
|                                                |  11:  64 WORKFLOW_TASK_STARTED
|18:   64 NEXUS_OPERATION_CANCEL_REQUEST_COMPLET |  <-- Cancel completed at 64ms
|                                                |  12:  93 WORKFLOW_TASK_COMPLETED
|                                                |  13:  93 WORKFLOW_EXECUTION_CANCELED
|22:   97 NEXUS_OPERATION_CANCELED               |
|23:   97 WORKFLOW_TASK_SCHEDULED                |
|24:   99 WORKFLOW_TASK_STARTED                  |
|26:  100 WORKFLOW_EXECUTION_SIGNALED            |  <-- Signal sent immediately after future resolved
|25:  103 WORKFLOW_TASK_COMPLETED                |
|27:  103 WORKFLOW_TASK_SCHEDULED                |
|28:  103 WORKFLOW_TASK_STARTED                  |
|29:  107 WORKFLOW_TASK_COMPLETED                |
|30:  107 WORKFLOW_EXECUTION_COMPLETED           |
```

## Key Observation

The critical difference is the timing of when `caller_op_future_resolved` is set (line 192-194 in the test):

**Failing case**: The future is resolved 2+ seconds AFTER the cancel request event
**Passing case**: The future is resolved immediately (before the next workflow task)

Looking at the histories, the signal to the handler workflow (`WORKFLOW_EXECUTION_SIGNALED` at event 21) happens at 64ms in the failing case, which is much later than expected. This signal is sent right after the future is resolved (lines 202-209), so the 2+ second delay is happening before the exception handler runs.

## The Real Issue

The test captures `datetime.now(timezone.utc)` when the exception handler runs (line 193), but in the failing case, there's a 2+ second delay before this code is reached. This suggests:

1. The activation that should resolve the Python future immediately is being delayed on Windows/ARM
2. The SDK Core is correctly writing `NEXUS_OPERATION_CANCEL_REQUESTED` immediately
3. But the Python code doesn't get to handle the cancellation for 2+ seconds

This is likely a bug in how activations are processed on certain platforms when handling TRY_CANCEL nexus operations.