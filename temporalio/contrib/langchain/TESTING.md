# Temporal LangChain Integration — Testing Plan

## 1 Overview
This document outlines the testing strategy for the Temporal LangChain integration following a test-driven development (TDD) approach. Tests are organized from basic functionality to complex integration scenarios, ensuring each component works correctly before building upon it.

---

## 2 Testing Phases

### Phase 1: Core Infrastructure Tests
**Objective**: Verify basic wrapper and activity functionality before adding complexity.

### Phase 2: Callback System Tests  
**Objective**: Ensure callback splitting and replay mechanisms work correctly.

### Phase 3: Integration Tests
**Objective**: Test complete workflows with real LangChain components.

### Phase 4: Advanced Feature Tests
**Objective**: Verify tracing, search attributes, and edge cases.

---

## 3 Phase 1: Core Infrastructure Tests

### 3.1 Static Activity Registration Tests
**Purpose**: Basic smoke test to verify activities can be registered by workers.

**Test: Worker Registration Smoke Test**
- **What**: Verify that a worker can start successfully with `get_wrapper_activities()` registered
- **Why**: Ensures the basic activity registry works and activities have proper decorators
- **Verification**: Worker starts without errors, activities are discoverable

### 3.2 Model Wrapper Basic Tests  
**Purpose**: Test fundamental model wrapping functionality.

**Test: Wrapper Creation**
- **What**: Create `TemporalModelWrapper` with a mock LangChain model
- **Why**: Verifies wrapper can be instantiated and basic validation works
- **Verification**: Wrapper accepts models with `ainvoke` or `invoke` methods, rejects invalid models

**Test: Interface Preservation**
- **What**: Check that wrapper exposes the same public methods and properties as the wrapped model
- **Why**: Ensures LangChain code can use wrapped models without modification
- **Verification**: `hasattr(wrapper, method_name)` returns same results as original model

**Test: Model Serialization in Workflow Context**
- **What**: Test wrapped model execution inside a Temporal workflow using `WorkflowEnvironment`
- **Why**: Verifies models can be serialized and executed in the real workflow replay context
- **Verification**: Workflow completes successfully, model call appears in activity history

### 3.3 Tool Wrapper Execution Tests
**Purpose**: Test tool wrapping and execution with various argument patterns.

**Test: Tool Wrapper Creation**
- **What**: Create `TemporalToolWrapper` with various LangChain tools
- **Why**: Ensures tool wrapper handles different tool types correctly
- **Verification**: Wrapper preserves tool name, description, args_schema properties

**Test: Tool Execution Patterns**
- **What**: Execute wrapped tools with: (1) no arguments, (2) positional arguments, (3) keyword arguments including complex types (dict, list)
- **Why**: Ensures arg-schema handling works correctly for different call patterns
- **Verification**: All execution patterns succeed and return expected results

**Test: Tool Interface Inheritance**
- **What**: Verify wrapper inherits from `BaseTool` and implements required methods
- **Why**: Ensures tools work with LangChain's tool execution framework
- **Verification**: Wrapper is instance of `BaseTool` and has both `_arun` and `ainvoke` methods

### 3.4 Activity Execution in Workflow Context
**Purpose**: Test activity input/output handling with real Temporal execution.

**Test: Activity Input/Output Serialization**
- **What**: Use `WorkflowEnvironment` to execute model and tool activities with real Temporal mechanics
- **Why**: Validates input/output serialization works in actual workflow context
- **Verification**: Activities execute successfully, inputs/outputs serialize correctly

**Test: Type Validation in Activities**
- **What**: Test that activities validate model/tool types using fully qualified class names
- **Why**: Ensures type safety and prevents runtime errors when models are deserialized
- **Verification**: Activities reject mismatched types with clear error messages

**Test: ModelOutput Standardization**
- **What**: Verify that model responses are properly converted to `ModelOutput` format
- **Why**: Ensures consistent response structure across different model providers
- **Verification**: All model responses include content, optional tool_calls, usage_metadata, and response_metadata

**Test: Workflow-Scoped Activity-as-Tool**
- **What**: Test `workflow.activity_as_tool()` method converts activities to LangChain tool specs within workflow context
- **Why**: Ensures bidirectional integration works with proper workflow scoping
- **Verification**: Converted tools execute correctly and maintain workflow determinism

---

## 4 Phase 2: Callback System Tests

### 4.1 Callback Splitting Tests
**Purpose**: Verify callbacks are correctly separated into activity and workflow categories.

**Test: Default Callback Handling**
- **What**: Test wrapper behavior when no callbacks are provided
- **Why**: Ensures system works without callbacks (common case)
- **Verification**: Activities receive empty callback list, no workflow callbacks executed

**Test: Activity Callback Forwarding**
- **What**: Verify callbacks from `RunnableConfig` are sent to activities
- **Why**: Ensures I/O-safe callbacks (logging, metrics) work in activities
- **Verification**: Activity input contains callbacks from config parameter

**Test: Workflow Callback Registration**
- **What**: Test `add_workflow_callback()` and `workflow_callbacks=` parameter
- **Why**: Ensures deterministic callbacks can be registered for workflow execution
- **Verification**: Workflow callbacks stored separately and not sent to activities

### 4.2 Callback Event Capture Tests
**Purpose**: Test callback event recording and replay mechanism.

**Test: Comprehensive Callback Event Capture**
- **What**: Verify all callback event types are captured (LLM, chain, tool, agent, text events)
- **Why**: Ensures complete callback event coverage for proper workflow replay
- **Verification**: `CallOutput.callback_events` contains all event types with proper serialization

**Test: Callback Type-Specific Handling**
- **What**: Test specific callback events: `on_llm_start`, `on_chat_model_start`, `on_tool_start`, `on_agent_action`, etc.
- **Why**: Ensures each callback type is properly captured and replayed
- **Verification**: Each callback type appears in captured events with correct parameters

**Test: Event Replay in Workflow**
- **What**: Test that captured events are properly replayed through workflow callbacks
- **Why**: Ensures workflow callbacks receive the same events as activity callbacks
- **Verification**: Workflow callbacks receive events in correct order with proper data

**Test: Callback Error Isolation**
- **What**: Verify callback failures don't break activity execution
- **Why**: Ensures robust execution when callbacks have bugs
- **Verification**: Activity completes successfully even if callbacks raise exceptions

---

## 5 Phase 3: Integration Tests

### 5.1 Mock Model Integration Tests
**Purpose**: Test complete workflow execution with mock models and the Temporal testing framework.

**Test: Simple Model Invocation Workflow**
- **What**: Create a workflow that calls a wrapped mock model's `ainvoke` method
- **Why**: Tests end-to-end execution through Temporal's activity system
- **Verification**: Workflow completes, returns expected result, activity appears in execution history

**Test: Model Method Forwarding Workflow**
- **What**: Test workflow calling additional methods such as `predict`, `batch`, and `predict_messages` to exercise attribute forwarding
- **Why**: Ensures `__getattr__` forwarding works through activity execution for all common model methods
- **Verification**: All forwarded methods execute as activities and return correct results

**Test: Sync Method Execution**
- **What**: Test workflow calling synchronous model methods through wrapper
- **Why**: Verifies `asyncio.to_thread` execution path works correctly
- **Verification**: Sync methods complete without blocking workflow thread

### 5.2 Mock Tool Integration Tests
**Purpose**: Test tool execution through Temporal activities.

**Test: Async Tool Execution Workflow**
- **What**: Create workflow that executes wrapped async tools with various inputs
- **Why**: Tests async tool activity execution and result handling
- **Verification**: Tool produces expected outputs, proper activity execution

**Test: Sync Tool Execution Workflow**
- **What**: Test workflow executing sync tools through `asyncio.to_thread` mechanism
- **Why**: Verifies sync tools work correctly without blocking workflow thread
- **Verification**: Sync tools complete successfully, executed via activity thread pool

**Test: Tool Sync Method Restriction**
- **What**: Test that calling `_run()` directly from workflow raises appropriate error
- **Why**: Ensures sync methods cannot be called directly from workflow context
- **Verification**: `NotImplementedError` raised with clear explanation about using `_arun`

**Test: Tool Method Detection**
- **What**: Test wrapper's logic for choosing between `_arun` and `_run` methods
- **Why**: Ensures proper method selection based on tool capabilities
- **Verification**: Wrapper correctly identifies and uses appropriate method

**Test: Activity-as-Tool Conversion**
- **What**: Test converting Temporal activities to LangChain tool specifications using `workflow.activity_as_tool()`
- **Why**: Ensures bidirectional integration between Temporal and LangChain
- **Verification**: Converted tools have proper schema and execute correctly

### 5.3 Agent Workflow Tests
**Purpose**: Test complete LangChain agent scenarios with wrapped components.

**Test: Simple Agent Execution**
- **What**: Run `AgentExecutor` with wrapped model and tools in a workflow
- **Why**: Tests realistic usage pattern with minimal LangChain modification
- **Verification**: Agent completes task, makes appropriate model/tool calls

**Test: Multi-Step Agent Reasoning**
- **What**: Test agent that requires multiple model calls and tool executions
- **Why**: Verifies complex interaction patterns work correctly
- **Verification**: Agent reasoning chain executes properly with deterministic replay

---

## 6 Phase 4: Advanced Feature Tests

### 6.1 Search Attribute Tests
**Purpose**: Verify model metadata is properly recorded in workflow search attributes.

**Test: Model Name Upsert**
- **What**: Verify `llm.model_name` search attribute is set before model calls
- **Why**: Ensures workflows can be queried by model type in Temporal Web
- **Verification**: Search attribute appears in workflow execution with correct model name

**Test: Multiple Model Tracking**
- **What**: Test workflow using multiple different wrapped models
- **Why**: Ensures search attributes are updated correctly for each model
- **Verification**: Search attribute reflects the last model used

### 6.2 Tracing Integration Tests
**Purpose**: Test OpenTelemetry context propagation through activities.

**Test: Trace Context Header Injection**
- **What**: Verify tracing context is properly injected into activity headers as JSON
- **Why**: Ensures tracing context can be propagated across Temporal boundaries
- **Verification**: Activity headers contain "otel-trace-context" with valid trace data

**Test: Trace Context Extraction**
- **What**: Test that activities extract and use parent trace context from headers
- **Why**: Ensures activity spans appear as children of workflow spans
- **Verification**: Activity spans have correct parent-child relationships in traces

**Test: Trace Span Attributes**
- **What**: Verify activity spans include proper Temporal metadata (activity name, type)
- **Why**: Ensures tracing spans are properly annotated for observability
- **Verification**: Spans include "temporal.activity.name" and "temporal.activity.type" attributes

**Test: Interceptor Integration**
- **What**: Test that tracing interceptor works with worker and client
- **Why**: Ensures tracing setup is correct and non-intrusive
- **Verification**: Traces appear without affecting functionality

### 6.3 Error Handling Tests
**Purpose**: Verify robust error handling in various failure scenarios.

**Test: Model Serialization Size Limits**
- **What**: Test behavior when models exceed serialization size limits
- **Why**: Ensures clear error messages guide users to solutions
- **Verification**: Helpful error messages explain serialization requirements and suggest alternatives

**Test: Activity Type Validation**
- **What**: Test activity execution with incorrect model/tool types using fully qualified class names
- **Why**: Ensures type safety and prevents runtime errors in activities
- **Verification**: Type mismatches raise clear validation errors with expected vs actual types

**Test: Unpicklable Object Handling**
- **What**: Test wrapper behavior when models/tools contain unpicklable objects
- **Why**: Ensures clear error messages help users identify serialization issues
- **Verification**: Serialization errors provide actionable guidance on fixing object composition

**Test: Activity Timeout and Retry Behavior**
- **What**: Test wrapper behavior when activities timeout or fail with retryable errors
- **Why**: Ensures Temporal's retry policies work correctly with wrapped components
- **Verification**: Activities retry according to configured policies, eventual success or failure

**Test: Workflow Cancellation During Model Call**
- **What**: Test workflow cancellation while a model activity is executing
- **Why**: Ensures cancellation is handled gracefully without corrupting state
- **Verification**: Cancellation propagates correctly, no resource leaks

**Test: Non-Retryable Provider Errors**
- **What**: Test handling of provider errors that shouldn't be retried (e.g., 4xx HTTP errors)
- **Why**: Ensures provider errors bubble up correctly without unnecessary retries
- **Verification**: Non-retryable errors fail immediately with original error details

**Test: Callback Exception Isolation**
- **What**: Test behavior when workflow or activity callbacks raise exceptions
- **Why**: Ensures callback errors don't break activity execution or workflow state
- **Verification**: Activity/workflow continues successfully even when callbacks fail

**Test: Activity Registration Validation**
- **What**: Test `workflow.activity_as_tool()` with non-activity functions
- **Why**: Ensures proper validation of activity decoration requirements
- **Verification**: `ApplicationError` raised for functions without `@activity.defn` decorator

---

## 7 Real Provider Tests

### 7.1 OpenAI Integration Tests
**Purpose**: Test with real OpenAI models (when API keys available).

**Test: ChatOpenAI Wrapper**
- **What**: Test wrapping and executing real OpenAI model calls
- **Why**: Verifies integration works with actual production models
- **Verification**: Real API calls succeed through wrapper with proper responses

**Test: OpenAI Tool Usage**
- **What**: Test OpenAI function calling through wrapped models
- **Why**: Ensures tool binding and execution works with real providers
- **Verification**: Function calls execute correctly with proper tool responses

### 7.2 Local Model Tests
**Purpose**: Test with local/open-source models.

**Test: Ollama Integration**
- **What**: Test wrapper with locally-hosted models
- **Why**: Ensures integration works beyond cloud providers
- **Verification**: Local model calls execute successfully through wrapper

---

## 8 Test Implementation Strategy

### 8.1 Test-Driven Development Flow
1. **Write failing test** describing expected behavior
2. **Implement minimal code** to make test pass
3. **Refactor** for clarity and maintainability
4. **Add next test** building on previous functionality

### 8.2 Mock Strategy
- **Phase 1**: Use `WorkflowEnvironment` with simple mock objects for models/tools
- **Phase 2**: Use realistic mocks that simulate real behavior patterns
- **Phase 3**: Include real provider tests where possible

### 8.3 Test Environment Setup
- Use Temporal testing framework (`WorkflowEnvironment`) for all workflow execution
- Mock external API calls unless specifically testing real providers
- Ensure tests are deterministic and repeatable
- Focus on functional correctness over performance benchmarks

---

## 9 Success Criteria

### 9.1 Functional Success
- All wrapper interfaces work identically to original LangChain objects
- Callbacks execute in appropriate contexts (workflow vs. activity)
- Search attributes and tracing work correctly
- Error messages are clear and actionable

### 9.2 Performance Success
- Wrapper overhead is negligible compared to model execution time
- Memory usage remains reasonable for typical model sizes
- Basic performance regression detection through optional benchmarks

### 9.3 Integration Success
- Real LangChain agents work with minimal code changes
- Common model providers (OpenAI, Anthropic) work correctly
- Complex agent reasoning patterns execute reliably

---

*End of testing plan.* 