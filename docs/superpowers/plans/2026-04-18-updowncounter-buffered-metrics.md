# UpDownCounter in Buffered Metrics — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add UpDownCounter (signed-delta counter) support to the Python SDK's Buffered Metrics pipeline and `Runtime.metric_meter`, matching sdk-typescript PR #2007.

**Architecture:** Thin pyo3 bridge wrapping `temporalio_common::metrics::UpDownCounter`, surfaced as a new `MetricUpDownCounter` ABC in `temporalio.common`, with a concrete implementation on the runtime meter. A new buffered-metric kind constant (`BUFFERED_METRIC_KIND_UP_DOWN_COUNTER = 3`) parallels the existing counter/gauge/histogram kinds. Workflow, activity, and nexus context meters are intentionally untouched — the ABC's `create_up_down_counter` default raises `NotImplementedError` to mirror the TS "optional" method.

**Tech Stack:** Rust + pyo3 + maturin for the bridge, Python 3 for the wrappers/ABC, `pytest` + `uv` + `poethepoet` for the build/test loop.

**Reference commits (sdk-typescript):** `69118e3f`, `85b088fd`, `e35fe782`.

**Spec:** `docs/superpowers/specs/2026-04-18-updowncounter-buffered-metrics-design.md`.

---

## Chunk 1: Failing test + Rust bridge

### Task 1: Write a failing buffered-metrics test for UpDownCounter

**Files:**
- Modify: `tests/worker/test_workflow.py` (extend imports near line ~100 and add a new test near existing `test_runtime_buffered_metrics`)

This test drives out the whole surface: `runtime.metric_meter.create_up_down_counter`, `add()` accepting negative values, `with_additional_attributes`, and the buffered update kind.

- [ ] **Step 1: Add the new buffered-metric-kind constant to the test imports**

Find the existing import block near line 104:

```python
from temporalio.runtime import (
    ...
    BUFFERED_METRIC_KIND_COUNTER,
    BUFFERED_METRIC_KIND_HISTOGRAM,
    ...
)
```

Add `BUFFERED_METRIC_KIND_UP_DOWN_COUNTER` alongside the other kind constants in that import.

- [ ] **Step 2: Add a new test function**

Place this test immediately after `test_runtime_buffered_metrics` in `tests/worker/test_workflow.py`. It is standalone — does not need a Temporal server, just a runtime with a metric buffer.

```python
async def test_runtime_buffered_metrics_up_down_counter() -> None:
    # Create runtime with metric buffer
    buffer = MetricBuffer(10000)
    runtime = Runtime(telemetry=TelemetryConfig(metrics=buffer))

    # Confirm no updates yet
    assert not buffer.retrieve_updates()

    # Create an up-down counter and a sibling with extra attrs
    up_down = runtime.metric_meter.create_up_down_counter(
        "runtime-up-down-counter",
        "runtime-up-down-counter-desc",
        "runtime-up-down-counter-unit",
    )
    up_down_with_attrs = up_down.with_additional_attributes({"foo": "bar"})

    # Emit a positive delta, a larger positive delta, and a negative delta
    up_down.add(1)
    up_down.add(5)
    up_down_with_attrs.add(-3)

    updates = buffer.retrieve_updates()
    assert len(updates) == 3

    # Metric metadata
    assert updates[0].metric.name == "runtime-up-down-counter"
    assert updates[0].metric.description == "runtime-up-down-counter-desc"
    assert updates[0].metric.unit == "runtime-up-down-counter-unit"
    assert updates[0].metric.kind == BUFFERED_METRIC_KIND_UP_DOWN_COUNTER
    # Exact-same metric object across updates (performance invariant)
    assert id(updates[0].metric) == id(updates[1].metric)
    assert id(updates[0].metric) == id(updates[2].metric)

    # Values include the negative delta
    assert updates[0].value == 1
    assert updates[1].value == 5
    assert updates[2].value == -3

    # Attributes match
    assert updates[0].attributes == {"service_name": "temporal-core-sdk"}
    assert updates[2].attributes == {
        "service_name": "temporal-core-sdk",
        "foo": "bar",
    }
```

- [ ] **Step 3: Run the test to verify it fails**

Run:
```bash
uv run pytest tests/worker/test_workflow.py::test_runtime_buffered_metrics_up_down_counter -v
```
Expected: `ImportError` on `BUFFERED_METRIC_KIND_UP_DOWN_COUNTER`, or `AttributeError: ... has no attribute 'create_up_down_counter'` once that import is resolved.

- [ ] **Step 4: Commit the failing test**

```bash
git add tests/worker/test_workflow.py
git commit -m "test: add failing buffered-metrics UpDownCounter test"
```

---

### Task 2: Add `UpDownCounter` to the Rust bridge

**Files:**
- Modify: `temporalio/bridge/src/metric.rs`

`convert_metric_event` already handles `MetricKind::UpDownCounter` (kind=3) and `MetricUpdateVal::SignedDelta` — no changes there. We only need a pyclass + a meter method + an `add(i64, ...)` method.

- [ ] **Step 1: Add the `MetricUpDownCounterRef` pyclass**

In `temporalio/bridge/src/metric.rs`, alongside the other ref structs (after `MetricGaugeFloatRef` at ~line 56):

```rust
#[pyclass]
pub struct MetricUpDownCounterRef {
    counter: metrics::UpDownCounter,
}
```

- [ ] **Step 2: Add `new_up_down_counter` to `MetricMeterRef`**

Inside `#[pymethods] impl MetricMeterRef { ... }` (after `new_gauge_float` at ~line 155), add:

```rust
    fn new_up_down_counter(
        &self,
        name: String,
        description: Option<String>,
        unit: Option<String>,
    ) -> MetricUpDownCounterRef {
        MetricUpDownCounterRef {
            counter: self
                .meter
                .up_down_counter(build_metric_parameters(name, description, unit)),
        }
    }
```

- [ ] **Step 3: Add the `add` impl for `MetricUpDownCounterRef`**

After the `MetricGaugeFloatRef` `#[pymethods]` block (around line 199), add:

```rust
#[pymethods]
impl MetricUpDownCounterRef {
    fn add(&self, value: i64, attrs_ref: &MetricAttributesRef) {
        self.counter.add(value, &attrs_ref.attrs);
    }
}
```

Note: value is `i64` (signed), **not** `u64`.

- [ ] **Step 4: Run `cargo fmt` and `bridge-lint`**

```bash
uv run poe bridge-lint
(cd temporalio/bridge && cargo fmt)
```
Expected: no warnings, clean format.

- [ ] **Step 5: Build the bridge**

```bash
uv run poe build-develop
```
Expected: successful compile; `temporal_sdk_bridge` rebuilt.

- [ ] **Step 6: Commit the Rust bridge changes**

```bash
git add temporalio/bridge/src/metric.rs
git commit -m "bridge: expose UpDownCounter via MetricMeterRef"
```

---

## Chunk 2: Python layers

### Task 3: Add the Python bridge wrapper

**Files:**
- Modify: `temporalio/bridge/metric.py`

- [ ] **Step 1: Add `MetricUpDownCounter` wrapper class**

After the `MetricCounter` class (after line 56), insert:

```python
class MetricUpDownCounter:
    """Metric up-down counter using SDK Core."""

    def __init__(
        self,
        meter: MetricMeter,
        name: str,
        description: str | None,
        unit: str | None,
    ) -> None:
        """Initialize up-down counter metric."""
        self._ref = meter._ref.new_up_down_counter(name, description, unit)

    def add(self, value: int, attrs: MetricAttributes) -> None:
        """Add value to up-down counter.

        Value may be negative.
        """
        self._ref.add(value, attrs._ref)
```

Note: no `if value < 0: raise ValueError(...)` — the whole point of an up-down counter is to accept negatives.

- [ ] **Step 2: Verify the module imports cleanly**

```bash
uv run python -c "import temporalio.bridge.metric; print(temporalio.bridge.metric.MetricUpDownCounter)"
```
Expected: prints the class, no errors.

- [ ] **Step 3: Commit**

```bash
git add temporalio/bridge/metric.py
git commit -m "bridge: add Python MetricUpDownCounter wrapper"
```

---

### Task 4: Extend the public ABC in `temporalio.common`

**Files:**
- Modify: `temporalio/common.py`

- [ ] **Step 1: Add the `MetricUpDownCounter` ABC**

In `temporalio/common.py`, after `MetricGaugeFloat` (around line 895), add:

```python
class MetricUpDownCounter(MetricCommon):
    """Up-down counter metric created by a metric meter."""

    @abstractmethod
    def add(
        self, value: int, additional_attributes: MetricAttributes | None = None
    ) -> None:
        """Add a value to the up-down counter.

        Value may be negative.

        Args:
            value: An integer to add (can be positive or negative).
            additional_attributes: Additional attributes to append to the
                current set.

        Raises:
            TypeError: Attribute values are not the expected type.
        """
        ...
```

- [ ] **Step 2: Add `create_up_down_counter` to the `MetricMeter` ABC**

Inside `class MetricMeter(ABC):` (near line 707, right before `with_additional_attributes`), add a **non-abstract, default-raising** method. This mirrors the TS optional `createUpDownCounter?`.

```python
    # FIXME: Make this abstract once up-down-counter support is complete
    # on every MetricMeter implementation.
    def create_up_down_counter(
        self, name: str, description: str | None = None, unit: str | None = None
    ) -> MetricUpDownCounter:
        """Create an up-down counter metric that accepts signed values.

        Args:
            name: Name for the metric.
            description: Optional description for the metric.
            unit: Optional unit for the metric.

        Returns:
            Up-down counter metric.

        Raises:
            NotImplementedError: If this meter does not support up-down
                counters (e.g. workflow or activity context meters).
        """
        raise NotImplementedError(
            "create_up_down_counter is not supported by this MetricMeter"
        )
```

- [ ] **Step 3: Add the noop `_NoopMetricUpDownCounter` + override on `_NoopMetricMeter`**

After `_NoopMetricGaugeFloat` (around line 998), add:

```python
class _NoopMetricUpDownCounter(MetricUpDownCounter, _NoopMetric):
    def add(
        self, value: int, additional_attributes: MetricAttributes | None = None
    ) -> None:
        pass
```

Inside `class _NoopMetricMeter(MetricMeter):` (around line 927, alongside the other `create_*` methods), add an override:

```python
    def create_up_down_counter(
        self, name: str, description: str | None = None, unit: str | None = None
    ) -> MetricUpDownCounter:
        return _NoopMetricUpDownCounter(name, description, unit)
```

- [ ] **Step 4: Syntax check**

```bash
uv run python -c "from temporalio.common import MetricMeter, MetricUpDownCounter; m = MetricMeter.noop; print(m.create_up_down_counter('x'))"
```
Expected: prints a `_NoopMetricUpDownCounter` instance without error.

- [ ] **Step 5: Confirm the default raises on a stub subclass**

```bash
uv run python -c "
from temporalio.common import MetricMeter
class Stub(MetricMeter):
    def create_counter(self, *a, **k): ...
    def create_histogram(self, *a, **k): ...
    def create_histogram_float(self, *a, **k): ...
    def create_histogram_timedelta(self, *a, **k): ...
    def create_gauge(self, *a, **k): ...
    def create_gauge_float(self, *a, **k): ...
    def with_additional_attributes(self, *a, **k): ...
try:
    Stub().create_up_down_counter('x')
except NotImplementedError as e:
    print('OK:', e)
"
```
Expected: prints `OK: create_up_down_counter is not supported by this MetricMeter`.

- [ ] **Step 6: Commit**

```bash
git add temporalio/common.py
git commit -m "common: add MetricUpDownCounter ABC and noop impl"
```

---

### Task 5: Wire UpDownCounter through the runtime meter

**Files:**
- Modify: `temporalio/runtime.py`

- [ ] **Step 1: Add the buffered-metric-kind constant and update docstring**

After `BUFFERED_METRIC_KIND_HISTOGRAM` (around line 518):

```python
BUFFERED_METRIC_KIND_UP_DOWN_COUNTER = BufferedMetricKind(3)
"""Buffered metric is an up-down counter which means values are signed deltas."""
```

Update the `BufferedMetric.kind` docstring (around line 547-553) to include the new kind:

```python
    @property
    def kind(self) -> BufferedMetricKind:
        """Get the metric kind.

        This is one of :py:const:`BUFFERED_METRIC_KIND_COUNTER`,
        :py:const:`BUFFERED_METRIC_KIND_GAUGE`,
        :py:const:`BUFFERED_METRIC_KIND_HISTOGRAM`, or
        :py:const:`BUFFERED_METRIC_KIND_UP_DOWN_COUNTER`.
        """
        ...
```

- [ ] **Step 2: Add `_MetricUpDownCounter`**

After `_MetricGaugeFloat` (around line 836), add:

```python
class _MetricUpDownCounter(
    temporalio.common.MetricUpDownCounter,
    _MetricCommon[temporalio.bridge.metric.MetricUpDownCounter],
):
    def add(
        self,
        value: int,
        additional_attributes: temporalio.common.MetricAttributes | None = None,
    ) -> None:
        core_attrs = self._core_attrs
        if additional_attributes:
            core_attrs = core_attrs.with_additional_attributes(additional_attributes)
        self._core_metric.add(value, core_attrs)
```

Note: intentionally **no** `if value < 0: raise ValueError(...)`.

- [ ] **Step 3: Implement `create_up_down_counter` on `_MetricMeter`**

Inside `class _MetricMeter(temporalio.common.MetricMeter):` (after `create_gauge_float` around line 680, before `with_additional_attributes`):

```python
    def create_up_down_counter(
        self, name: str, description: str | None = None, unit: str | None = None
    ) -> temporalio.common.MetricUpDownCounter:
        return _MetricUpDownCounter(
            name,
            description,
            unit,
            temporalio.bridge.metric.MetricUpDownCounter(
                self._core_meter, name, description, unit
            ),
            self._core_attrs,
        )
```

- [ ] **Step 4: Syntax + light import check**

```bash
uv run python -c "
from temporalio.runtime import (
    BUFFERED_METRIC_KIND_UP_DOWN_COUNTER,
    Runtime, TelemetryConfig, MetricBuffer,
)
print(BUFFERED_METRIC_KIND_UP_DOWN_COUNTER)
"
```
Expected: prints `3`.

- [ ] **Step 5: Commit**

```bash
git add temporalio/runtime.py
git commit -m "runtime: expose UpDownCounter via Runtime.metric_meter"
```

---

## Chunk 3: Verification + polish

### Task 6: Run the test end-to-end

- [ ] **Step 1: Rebuild the bridge (picks up Rust changes)**

```bash
uv run poe build-develop
```
Expected: successful compile.

- [ ] **Step 2: Run the new test**

```bash
uv run pytest tests/worker/test_workflow.py::test_runtime_buffered_metrics_up_down_counter -v
```
Expected: PASS.

- [ ] **Step 3: Run the existing buffered-metrics test to confirm no regression**

```bash
uv run pytest tests/worker/test_workflow.py::test_runtime_buffered_metrics -v
```
Expected: PASS (unchanged behavior).

- [ ] **Step 4: If either test fails, debug via superpowers:systematic-debugging before moving on**

---

### Task 7: Lint and format

- [ ] **Step 1: Ruff format/lint the Python**

```bash
uv run poe format
uv run poe lint
```
Expected: no errors.

- [ ] **Step 2: Clippy the bridge**

```bash
uv run poe bridge-lint
```
Expected: no warnings.

- [ ] **Step 3: If lint introduced any changes, commit them**

```bash
git add -u
git diff --cached --quiet || git commit -m "chore: lint and format"
```

---

### Task 8: Final review before handoff

- [ ] **Step 1: Diff the full branch against main**

```bash
git diff --stat main...HEAD
git log --oneline main..HEAD
```

Expected files changed (non-exhaustive):
- `docs/superpowers/specs/2026-04-18-updowncounter-buffered-metrics-design.md`
- `docs/superpowers/plans/2026-04-18-updowncounter-buffered-metrics.md`
- `temporalio/bridge/src/metric.rs`
- `temporalio/bridge/metric.py`
- `temporalio/common.py`
- `temporalio/runtime.py`
- `tests/worker/test_workflow.py`

- [ ] **Step 2: Use superpowers:verification-before-completion before claiming done**

Specifically: re-run the new test in isolation, then confirm `uv run poe lint` and `uv run poe bridge-lint` are clean.

- [ ] **Step 3: Report status to user** with:
  - Test output for the new test (pass)
  - Summary of files changed
  - Any deviations from the plan

---

## Notes / pitfalls

- The Rust `add(value: i64, ...)` type is load-bearing — if this is left as `u64`, negative values will either wrap or fail silently depending on Python int marshalling.
- `convert_metric_event` already maps `SignedDelta` to `BufferedMetricUpdateValue::I64(v)` (which becomes a Python `int`), so no conversion work is needed in the Python test assertions — values come out as `int`, not `float`.
- The TS PR intentionally does not plumb UpDownCounter through workflow/activity context meters. Do not add `create_up_down_counter` to `_ReplaySafeMetricMeter` or to activity/nexus context meters. The ABC's default `NotImplementedError` is the correct behavior for those meters.
- `BUFFERED_METRIC_KIND_UP_DOWN_COUNTER = 3` must match the integer already emitted by `temporalio/bridge/src/metric.rs::convert_metric_event` (line 352). Verify before claiming done.
