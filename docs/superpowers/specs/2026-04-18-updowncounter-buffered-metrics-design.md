# Port: UpDownCounter in Buffered Metrics and Runtime MetricMeter

**Date:** 2026-04-18
**Branch:** `add_updowncounter_to_buffered_metrics`
**Upstream reference:** temporalio/sdk-typescript PR #2007 (commits `69118e3f`, `85b088fd`, `e35fe782`)

## Goal

Port the TypeScript SDK's UpDownCounter feature to the Python SDK. An UpDownCounter
is a metric instrument that accepts signed (positive or negative) delta values,
exposed through the Buffered Metrics pipeline and through `Runtime.metric_meter`.
This change intentionally does **not** plumb UpDownCounter into workflow,
activity, or nexus context meters — matching the upstream PR's scope.

## Background

`sdk-core` at the commit already pinned by sdk-python (`b544f95d`) exposes
`TemporalMeter::up_down_counter(...)`, the `UpDownCounter` core type, and the
`MetricUpdateVal::SignedDelta` / `MetricKind::UpDownCounter` variants. The
Python bridge's `convert_metric_event` already branches on both (kind=3,
`BufferedMetricUpdateValue::I64(v)`), but there is no Python-side API to
*create* an UpDownCounter or emit values through it.

Existing public surface that UpDownCounter mirrors:

- `temporalio.common.MetricCounter` / `temporalio.runtime._MetricCounter`
- `temporalio.bridge.metric.MetricCounter` / `temporalio.bridge.src.metric.MetricCounterRef`

## Non-goals

- Workflow, activity, and nexus context meters are out of scope (matches TS).
  These meters will raise `NotImplementedError` if `create_up_down_counter` is
  called on them.
- No changes to the sdk-core submodule pin.
- No OpenTelemetry / Prometheus config changes — the runtime already routes
  UpDownCounter updates through the buffered pipeline and the core meter.

## Design

### 1. Rust bridge (`temporalio/bridge/src/metric.rs`)

Add a new pyclass `MetricUpDownCounterRef` holding a `metrics::UpDownCounter`.
Add `MetricMeterRef::new_up_down_counter(name, description, unit)` delegating
to `self.meter.up_down_counter(build_metric_parameters(...))`. Add a
`MetricUpDownCounterRef::add(value: i64, attrs_ref)` method that calls
`self.counter.add(value, &attrs_ref.attrs)` — note the value is `i64`, not
`u64`, since the whole point is signed deltas.

No changes needed to `convert_metric_event` or `BufferedMetric::kind` — both
already handle `UpDownCounter` (kind=3) and `SignedDelta`.

### 2. Python bridge (`temporalio/bridge/metric.py`)

Add a `MetricUpDownCounter` wrapper class paralleling `MetricCounter`, but with
**no non-negative validation** in `add` (it must accept negative values):

```python
class MetricUpDownCounter:
    def __init__(self, meter, name, description, unit) -> None:
        self._ref = meter._ref.new_up_down_counter(name, description, unit)

    def add(self, value: int, attrs: MetricAttributes) -> None:
        self._ref.add(value, attrs._ref)
```

### 3. Public ABC (`temporalio/common.py`)

- Add `MetricUpDownCounter(MetricCommon)` ABC with an abstract `add(value: int,
  additional_attributes)` method. Unlike `MetricCounter`, its docstring does
  *not* require a non-negative value.
- Add `create_up_down_counter(name, description, unit) -> MetricUpDownCounter`
  to the `MetricMeter` ABC as a **concrete, non-abstract** method that raises
  `NotImplementedError` by default. This mirrors TS's optional `createUpDownCounter?`
  field and avoids breaking user subclasses of `MetricMeter`. A leading FIXME
  comment notes the default should be removed once support is complete on all
  known implementations.
- Add `_NoopMetricUpDownCounter` and override `create_up_down_counter` on
  `_NoopMetricMeter` to return it.

### 4. Runtime (`temporalio/runtime.py`)

- Add public constant `BUFFERED_METRIC_KIND_UP_DOWN_COUNTER = BufferedMetricKind(3)`.
- Update `BufferedMetric.kind` docstring to mention the new constant.
- Add `_MetricUpDownCounter(temporalio.common.MetricUpDownCounter,
  _MetricCommon[temporalio.bridge.metric.MetricUpDownCounter])` with `add()`
  that does **not** raise on negative values.
- Implement `_MetricMeter.create_up_down_counter(...)` returning
  `_MetricUpDownCounter(...)`.

### 5. Intentionally untouched

- `_ReplaySafeMetricMeter` in `temporalio/worker/_workflow_instance.py` — will
  inherit the ABC's default `NotImplementedError`.
- Nexus / activity context meters — same.

### 6. Testing

Extend `tests/worker/test_workflow.py::test_runtime_buffered_metrics`, or add
a new focused test in the same file, that:

- Creates an UpDownCounter via `runtime.metric_meter.create_up_down_counter(...)`.
- Emits a positive value, another positive value, and a negative value.
- Drains the buffer and asserts `BUFFERED_METRIC_KIND_UP_DOWN_COUNTER`, the
  metric name/description/unit, and the three values (including the negative).

## Open questions — confirmed with user

- `create_up_down_counter` is non-abstract with a `NotImplementedError` default
  on the ABC (non-breaking). **Confirmed.**
- Match TS scope: workflow / activity / nexus meters are untouched.
  **Confirmed.**
- Test location decided at implementation time — extend existing
  `test_runtime_buffered_metrics` or add a sibling test in the same file.

## Risks

- The ABC addition is non-breaking only if users call `create_up_down_counter`
  explicitly on meters that support it. Callers that treat every meter as
  having the method will hit `NotImplementedError` at runtime. Acceptable —
  matches TS semantics of "optional".
- The bridge adds a new NAPI-style pyo3 class; must be re-compiled (`maturin
  develop` or equivalent) before Python tests pass.
