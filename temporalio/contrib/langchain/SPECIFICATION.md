# Temporal LangChain Integration — Requirements Specification

## 1 Purpose
This document defines **what** the Temporal LangChain integration must provide for developers. It does **not** prescribe implementation details.

Temporal offers *durable execution*: workflow code must be deterministic, while non-deterministic work (network I/O, LLM calls, tool invocations) must execute in activities. The integration lets developers use LangChain models, tools and agents inside workflows while automatically routing the non-deterministic parts to activities.

---

## 2 Goals
1. **Seamless adoption** – Existing LangChain code runs in a workflow with minimal modification—just lightweight wrapper calls.  
2. **Durable execution split** – Deterministic workflow logic stays in the workflow thread; non-deterministic model/tool operations run as activities.  
3. **Configurable reliability** – All Temporal activity options (timeouts, retries, task-queue routing, priorities) are exposed.  
4. **Type-safe data contracts** – Public APIs are fully typed and Pydantic-compatible to support the `pydantic_data_converter`.  
5. **Observability & tracing** – Tracing context (OpenTelemetry) propagates so model/tool latency appears in Temporal Web and external trace viewers.  
6. **Broad compatibility** – Works with LangChain-core ≥ 0.1.0 and major providers (OpenAI, Anthropic, local models).

---

## 3 Non-Goals
• Supporting LangChain models that expose only experimental streaming APIs (deferred).  
• Multi-language SDK parity (this spec covers **Python** only).

---

## 4 Functional Requirements

### 4.1 Model Invocation
FR-1 `model_as_activity(model, **activity_params)` **shall**:
  a. Accept any LangChain model that implements `ainvoke` (async) or `invoke` (sync) and subclasses `BaseLanguageModel`.  
  b. Return a wrapper (`TemporalModelWrapper`) whose public surface is compatible with the original model (primary methods and properties).  
  c. Execute async calls directly; if the underlying method is synchronous, run it via `asyncio.to_thread` inside the activity.  
  d. When the caller does not pass `task_queue`, use the **workflow’s task queue** (Temporal default).  Developers can override per-model.

### 4.2 Tool Invocation
FR-2 `tool_as_activity(tool, **activity_params)` **shall** wrap a LangChain `BaseTool` so that its `ainvoke` (or `invoke` via `asyncio.to_thread`) runs as a Temporal activity.

FR-3 `workflow.activity_as_tool(activity_fn, **activity_params)` **shall** expose an existing Temporal activity as a LangChain tool specification (`name`, `description`, `args_schema`, `execute`).

### 4.3 Worker Integration
FR-4 `get_wrapper_activities()` **shall** return the static list `[model_activity, tool_activity]` so a worker can register wrappers with one call.

### 4.4 Telemetry / Tracing
FR-5 The integration **shall** propagate active OpenTelemetry `SpanContext` from workflow → activity and back using Temporal headers (pattern mirrors `OpenAIAgentsTracingInterceptor`).

### 4.5 Callbacks Handling
FR-6 Callbacks are split:
  • **Workflow callbacks** – Deterministic callbacks (e.g., upserting search attributes) provided via `workflow_callbacks=` kw-arg are executed in the workflow thread **after** the activity returns.  
  • **Activity callbacks** – Original LangChain `callbacks=[...]` list is forwarded to the activity where network I/O and logging are safe.  
The wrapper strips workflow-side callbacks from the payload sent to the activity and re-attaches them on return so downstream LangChain components receive a unified callback chain.

### 4.6 Search-Attribute Metadata
FR-7 Before invoking a model activity, the wrapper **shall** upsert a workflow search attribute `llm.model_name = <provider/model>` so that queries in Temporal Web can filter by model.

---

## 5 Non-Functional Requirements
NFR-1 **Durability correctness** – Workflow code must not perform I/O or other non-deterministic operations; wrappers enforce this separation.  
NFR-2 **Minimal overhead** – Wrapper call latency is negligible relative to model latency (target: ≪ 1 ms added in-process).  
NFR-3 **Observability naming** – Exactly two generic activity names are used:  
• `langchain_model_call` — model invocations  
• `langchain_tool_call`  — tool invocations  
`activity_as_tool` preserves the developer-supplied activity name.

---

## 6 Public API
| Symbol | Description |
|---|---|
| `model_as_activity(model, **activity_params)` | Wrap a LangChain model as an activity |
| `tool_as_activity(tool, **activity_params)` | Wrap a LangChain tool as an activity |
| `workflow.activity_as_tool(activity_fn, **activity_params)` | Expose an activity as a LangChain tool |
| `get_wrapper_activities()` | Return `[model_activity, tool_activity]` for worker registration |

`TemporalModelWrapper` and `TemporalToolWrapper` mirror core LangChain interfaces while transparently dispatching to Temporal activities.

`ModelOutput` (derived from LangChain response) contains `content: str`, optional `tool_calls: list`, optional `usage_metadata: dict`.

---

## 7 Developer Experience
• **One line per component** – Wrapping a model or tool requires a single call.  
• **≤ 10 LOC integration overhead** – ≤ 1 LOC per wrapped component plus under 10 LOC for worker configuration.  
• **Native patterns preserved** – Agents (`AgentExecutor`), chains, etc., work unchanged; wrappers are invisible at call-sites.

---

## 8 Deferred Enhancements
1. **Streaming token support** once Temporal introduces streaming payloads.  
2. **Batch invocation helpers** for high-throughput scenarios.  
3. **Thread-pool tuning** – expose configuration for the size/behaviour of the background executor used for sync `invoke` methods.

---

## 9 Success Metrics
1. **Adoption friction** – A sample LangChain agent (~50 LOC) migrates to Temporal in ≤ 10 new LOC.  
2. **Durability separation** – Automated tests confirm no network I/O occurs in the workflow thread.  
3. **Tracing propagation** – End-to-end trace shows parent workflow span → model/tool activity span in at least one OpenTelemetry exporter.

---

## 10 Open Questions
• Should the callbacks replay mechanism surface per-token events for streaming models once streaming is implemented?

---

## 11 Glossary
| Term | Meaning |
|------|---------|
| **Temporal activity** | External, retriable unit of work that may perform I/O; executes outside the deterministic workflow thread. |
| **Wrapper** | An adapter object (`TemporalModelWrapper` or `TemporalToolWrapper`) that proxies a LangChain model/tool and runs its methods in a Temporal activity. |
| **Workflow callbacks** | Deterministic callback functions executed inside the workflow thread after an activity completes. |
| **Activity callbacks** | Original LangChain callbacks executed inside the activity process; safe for I/O and logging. |
| **Search attribute** | Indexable key/value pair attached to a workflow for querying in Temporal Web. |

---

*End of requirements specification.* 