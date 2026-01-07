# Standalone Activity Implementation TODO

Comparison of spec (cross-sdk-design.md, Python section) against current PR implementation.

---

## 1. Evaluate `input` Field in `ActivityExecutionDescription`

**Spec Section:** 1
**Location:** `temporalio/client.py`, line ~3598

**Current:** Has `input: Sequence[Any]` field
**Spec:** Does not include this field

### Tasks:
- [ ] Confirm with spec owner whether `input` should be included
- [ ] If not needed, remove the field and update `_from_execution_info`
