# Agent SDK Integration Analysis

Based on analysis of existing integrations (OpenAI Agents SDK and Pydantic AI) and the plugin integrators guide, this document summarizes key patterns, best practices, and design decisions for integrating Agent SDKs with Temporal.

## Executive Summary

Agent SDK integrations with Temporal follow a consistent pattern:
1. **Plugin Architecture**: Use Temporal's plugin system for seamless integration
2. **Activity Isolation**: Move non-deterministic operations (model calls, tool execution) to activities
3. **Workflow Orchestration**: Keep agent logic and state management in workflows
4. **Serialization**: Handle complex data types with custom payload converters. Where possible, use the pydantic for serialization.
5. **Configuration**: Provide flexible activity configuration (timeouts, retries)

## Core Integration Patterns

### 1. Plugin-Based Architecture

Both integrations use Temporal's plugin system to provide:
- **Client Plugin**: Configures serialization and data converters
- **Worker Plugin**: Registers activities, configures interceptors, manages runtime overrides

```python
class AgentPlugin(ClientPlugin, WorkerPlugin):
    def configure_client(self, config: ClientConfig) -> ClientConfig:
        # Configure data converter for agent-specific types
        
    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        # Register activities, add interceptors
```

### 2. Deterministic/Non-Deterministic Separation

**Workflow (Deterministic)**:
- **Agent orchestration logic**: Coordinates between model calls, tool execution, and result processing. This includes the agent's main run loop, managing conversation flow, and handling handoffs between agents.
- **Decision making**: Deterministic logic like conditional branching based on model responses, routing to different tools based on intent classification, or deciding when to retry vs. escalate failures. This does NOT include the actual AI model inference (which goes in activities).
- **State management**: Tracking conversation history, user context, intermediate results, and workflow progress. All state updates must be deterministic.
- **Control flow**: Managing the sequence of operations, loops for iterative processing, error handling paths, and coordination between multiple agents or tools.

**Activities (Non-Deterministic)**:
- Model API calls
- Tool execution with I/O
- External service interactions

### 3. Activity Generation Patterns

**OpenAI Agents SDK Approach**:
- Single model activity that handles all LLM calls
- Tool activities created dynamically via `activity_as_tool()` helper
- Uses function inspection to generate activity schemas

**Pydantic AI Approach**:
- Wraps entire agent in `TemporalAgent` wrapper
- Automatically generates activities for model calls and toolsets
- Uses dependency injection for activity parameters

**Analysis: TemporalAgent Wrapper Approach**

The `TemporalAgent` wrapper in Pydantic AI represents a different philosophical approach compared to our preferred OpenAI Agents SDK style:

**Wrapper Pros:**
- Seamless migration from non-Temporal to Temporal agents
- Automatic activity generation reduces boilerplate
- Hides complexity of Temporal integration

**Wrapper Cons (Why we prefer explicit):**
- **Less Temporal-native**: Users don't write "real" Temporal workflows, they write agent code that happens to run on Temporal
- **Hidden magic**: Activity boundaries and configuration are not obvious to developers
- **Reduced control**: Harder to optimize individual activities or customize retry/timeout policies
- **Debugging complexity**: When things go wrong, developers must understand both agent semantics and hidden Temporal mechanics

**Our Preference: Explicit Workflow Design**
We prefer the OpenAI Agents SDK approach where users write explicit Temporal workflows that call agent functionality. This makes Temporal concepts (workflows, activities, timeouts) first-class and visible, leading to better understanding and more maintainable code.

## Key Implementation Details

### 1. Data Serialization

Both integrations address serialization challenges:
- Use Pydantic-based payload converters
- Handle complex agent-specific types (responses, contexts, tool outputs)
- Ensure 2MB payload limit compliance

```python
class _OpenAIPayloadConverter(PydanticPayloadConverter):
    def __init__(self) -> None:
        super().__init__(ToJsonOptions(exclude_unset=True))
```

**OpenAI Agents SDK Serialization Complexity**

The OpenAI Agents SDK uses a mixed type system that creates integration challenges:
- **Inputs**: Uses TypedDicts for function parameters and tool inputs
- **Outputs**: Uses Pydantic models for responses and structured data
- **Challenge**: Temporal's payload converter must handle both serialization formats
- **Solution**: The `_OpenAIPayloadConverter` extends `PydanticPayloadConverter` with `exclude_unset=True` to handle TypedDict serialization while maintaining Pydantic model support

This dual format requires careful handling in activity interfaces to ensure proper serialization/deserialization across the Temporal boundary.

### 2. Context Management

**Context Limitations in Activities**:
- Activities receive limited context (only serializable parts)
- Model, usage, prompt, messages not available in activities
- Custom serialization methods for extended context

**Context in Agent SDKs**

**OpenAI Agents SDK Context:**
Context (`RunContextWrapper`) provides tools and other code access to:
- Shared state across tool calls within a single agent run
- Agent configuration and settings
- Tracing and observability data
- Request metadata and headers

**Pydantic AI Context:**
Similar concept with `RunContext` that includes:
- Dependencies object (deps) for shared resources
- Usage tracking and token counting
- Model and prompt information
- Tracing integration

**Temporal Challenge:**
Both context systems expect in-memory, mutable shared state. In Temporal:
- Activities run in separate processes/workers
- Context must be serialized and passed to activities
- Context updates in activities cannot propagate back to workflow
- Solution: Serialize minimal context for activity use, maintain full context in workflow

**Pydantic AI Example**:
```python
class TemporalRunContext(RunContext[AgentDepsT]):
    @classmethod
    def serialize_run_context(cls, ctx: RunContext[AgentDepsT]) -> dict[str, Any]:
        # Only serialize specific fields for activity use
```

**Pydantic AI Context Serialization Example Explained**

This code shows how Pydantic AI handles the context serialization challenge:

```python
class TemporalRunContext(RunContext[AgentDepsT]):
    @classmethod
    def serialize_run_context(cls, ctx: RunContext[AgentDepsT]) -> dict[str, Any]:
        # Only serialize specific fields for activity use
```

**What This Solves:**
- The full `RunContext` contains non-serializable data (models, tracers, large message history)
- Activities need some context data but can't access everything
- This method creates a "lightweight" context with only serializable, essential fields

**Why Important:**
- Demonstrates the need for custom serialization strategies
- Shows how to balance context access vs. Temporal's serialization requirements
- Provides extension point for agent SDK authors to customize what context data activities receive

**Pattern for Integration:** Agent SDK plugins should provide similar context serialization mechanisms when activities need access to run-time state.

### 3. Activity Configuration

Flexible configuration for different activity types:
- Base configuration for all activities
- Model-specific configuration
- Tool-specific configuration
- Per-toolset configuration

```python
activity_config = ActivityConfig(start_to_close_timeout=timedelta(seconds=60))
model_activity_config = {...}  # Merged with base
tool_activity_config = {...}   # Per-tool overrides
```

### 4. Error Handling

**Non-Retryable Errors**:
- User errors (invalid input)
- Authentication failures
- Pydantic validation errors

```python
retry_policy.non_retryable_error_types = [
    UserError.__name__,
    PydanticUserError.__name__,
]
```

**Temporal-Controlled Retry Policy**

The OpenAI Agents SDK integration correctly delegates retry control to Temporal rather than letting the underlying HTTP client handle retries:

**Implementation Strategy:**
1. **Disable Client Retries**: Set `max_retries=0` on OpenAI client to prevent double-retry scenarios
2. **Preserve HTTP Context**: Activity exceptions wrap the original HTTP exceptions, preserving:
   - Status codes (429 for rate limits, 500 for server errors)
   - `Retry-After` headers for backoff timing
   - Error details for non-retryable failures (401 auth, 400 bad request)

**Benefits:**
- **Unified Retry Logic**: Temporal handles all retries consistently
- **Proper Backoff**: Temporal can respect `Retry-After` headers through custom retry policies
- **Better Observability**: All retry attempts appear in Temporal UI
- **Workflow Integration**: Retries integrate with workflow timeout and cancellation

**Configuration:**
```python
retry_policy = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=60),
    backoff_coefficient=2.0,
    non_retryable_error_types=["AuthenticationError", "InvalidRequestError"]
)
```

This pattern should be followed by all agent SDK integrations to ensure consistent, observable retry behavior.

### 5. Sandbox Configuration

**Workflow Sandbox Integration**:
```python
config['workflow_runner'] = replace(
    runner,
    restrictions=runner.restrictions.with_passthrough_modules(
        'pydantic_ai', 'logfire', 'rich', 'httpx', 'attrs', 'numpy', 'pandas'
    ),
)
```
**Workflow Sandbox Configuration**

The workflow sandbox restricts module imports to ensure deterministic execution. Agent SDK plugins must configure passthrough modules for their dependencies:

```python
config['workflow_runner'] = replace(
    runner,
    restrictions=runner.restrictions.with_passthrough_modules(
        'pydantic_ai', 'logfire', 'rich', 'httpx', 'attrs', 'numpy', 'pandas'
    ),
)
```

**How Library Authors Determine Modules:**

1. **Direct Dependencies**: Include your main package (e.g., 'pydantic_ai')
2. **Runtime Discovery**: Run workflows and watch for import errors like:
   ```
   ImportError: Module 'httpx' is not available in the sandbox
   ```
3. **Transitive Dependencies**: Include modules imported by your dependencies:
   - 'attrs': Used by Pydantic for dataclasses
   - 'numpy', 'pandas': Imported by logging/serialization libraries
   - 'httpx': HTTP client used by many AI SDKs

**Testing Strategy:**
- Run comprehensive test suites in sandbox mode
- Test with different Python environments (versions, packages)
- Monitor for circular import issues

**Best Practices:**
- Be conservative - include modules that might be imported dynamically
- Document required passthrough modules for users
- Consider providing a helper function that returns required modules


## Design Trade-offs

### OpenAI Agents SDK vs Pydantic AI

### OpenAI Agents SDK vs Pydantic AI

**⭐ Temporal Recommendation: We prefer the OpenAI Agents SDK integration style**

The OpenAI Agents SDK approach aligns better with Temporal's philosophy of explicit, understandable distributed system design:

| Aspect | OpenAI Agents SDK | Pydantic AI |
|--------|-------------------|-------------|
| **Integration Style** | Explicit activity conversion | Automatic wrapping |
| **Tool Handling** | Manual `activity_as_tool()` | Automatic toolset wrapping |
| **Configuration** | Activity-level parameters | Agent-level with inheritance |
| **Context Management** | Limited context in activities | Custom serialization support |
| **Streaming** | Not supported | Event handler approach |

### Key Strengths

**OpenAI Agents SDK**:
- Explicit control over activity boundaries
- Clear separation of concerns
- Granular timeout/retry configuration
- Direct mapping from tools to activities

**Pydantic AI**:
- Seamless agent wrapping
- Automatic activity generation
- Rich configuration inheritance
- Better streaming support via event handlers

## Common Challenges & Solutions

### 1. Streaming Support


**Challenge**: Activities can't stream directly to workflow
**Note**: Streaming guidance will be added in future iterations of this guide.

### 2. Tool Context Updates

**Challenge**: Activity tools can't update workflow context
**Solutions**:
- Read-only context in activities
- Return context updates as activity results
- Use workflow state for context management

**Context Updates Reality Check**

Neither implementation actually returns context updates as activity results:

**OpenAI Agents SDK**: Context in activities is read-only. Tools cannot update the shared context.

**Pydantic AI**: Similar limitation - context updates in activities don't propagate back to the workflow.

**Actual Solutions:**
- **Workflow State Management**: Store context updates as workflow variables
- **Activity Return Values**: Return relevant state changes that workflows can incorporate
- **External State**: Use external storage (databases, caches) when context sharing is essential

The "context updates as activity results" solution is theoretical but not implemented in either current integration.

### 3. Serialization Limits

**Challenge**: 2MB payload limit for Temporal
**Solutions**:
- Minimal context serialization
- External storage for large payloads
- Compression for large data

**Serialization Limits and Claim Check Pattern**

Neither current integration implements claim check patterns for large payloads, but this is a valuable pattern for agent SDKs:

**Claim Check Implementation:**
1. **Detection**: Check payload size before serialization
2. **Storage**: Store large objects (conversation history, images, documents) in external storage (S3, database)
3. **Reference**: Pass only storage keys/URLs through Temporal
4. **Retrieval**: Activities retrieve full objects using the reference

**When Needed:**
- Large conversation histories (>1MB)
- Image/file processing workflows  
- Embedded documents or knowledge bases
- Large model responses

**Implementation Consideration:**
Agent SDK integrations should provide optional claim check support for scenarios involving large payloads, with configurable size thresholds.

### 4. Replay Testing

**Challenge**: Ensuring deterministic behavior across versions
**Solutions**:
- Comprehensive replay test suites
- Patching for non-deterministic changes
- Version-aware activity naming

**Replay Testing: Critical Requirements for Agent SDK Plugin Authors**

**⚠️ Critical Understanding: Workflow Determinism**

Agent SDK plugin authors MUST understand that Temporal workflows replay from the beginning when:
- Workers restart
- Code is deployed
- Workflows are reset or continued

**Plugin Author Responsibilities:**

1. **Activity Naming Stability**: Once deployed, activity names cannot change
   ```python
   # BAD: Changing this breaks existing workflows
   @activity.defn(name="model_call_v2")  # Was "model_call"
   
   # GOOD: Stable naming
   @activity.defn(name="model_call")
   ```

2. **Deterministic Workflow Code**: Any code running in workflows must be deterministic
   - No randomness, timestamps, or file I/O
   - Same inputs always produce same outputs

3. **Schema Compatibility**: Activity inputs/outputs must remain compatible
   - Add optional fields, never remove required ones
   - Use versioning for breaking changes

4. **Replay Testing**: MUST test workflow compatibility across versions
   ```python
   # Test that old workflow histories replay with new code
   await replayer.replay_workflow(old_history)
   ```

**Consequences of Violation:**
- Non-determinism errors terminate running workflows
- Users lose long-running agent conversations
- Production downtime during deployments

**Required Testing:**
- Capture workflow histories before code changes
- Replay histories with new code versions
- Test with different workflow states and timings

## Integration Requirements

### 1. Agent SDK Requirements

For successful Temporal integration, Agent SDKs need:
- **Stable Naming**: Agent and tool names must be stable for production
- **Serializable State**: All passed data must be Pydantic-serializable
- **Deterministic Separation**: Clear boundaries between deterministic and non-deterministic operations
- **Error Classification**: Distinguish retryable vs non-retryable errors

**Verified Agent SDK Requirements**

✅ **Stable Naming**: Critical for Temporal workflow compatibility. Activity and workflow names become part of the execution history.

✅ **Serializable State**: All data passed between workflows and activities must be serializable. Pydantic is preferred for type safety and schema evolution.

✅ **Deterministic Separation**: Essential for replay reliability. AI model calls, I/O operations, and random number generation must be in activities.

✅ **Error Classification**: Correct retry behavior depends on distinguishing retryable (network timeouts, rate limits) from non-retryable errors (authentication, validation).

**Additional Requirement:**
- **Schema Evolution**: Design data models to support backward-compatible changes (optional fields, default values)

### 2. Plugin Architecture Requirements

**Client Plugin Must Provide**:
- Custom data converter configuration
- Serialization setup for agent types
- **Interceptor registration**: For tracing, metrics, or request/response modification
- **Connection configuration**: Custom service clients if needed

**Worker Plugin Must Provide**:
- Activity registration
- Interceptor configuration  
- Workflow sandbox configuration
- Error handling setup
- **Workflow registration**: If providing pre-built workflows
- **Nexus service handlers**: If supporting Nexus operations
- **Custom payload converters**: For worker-specific serialization needs

### 3. Activity Design Requirements

**Model Activities**:
- Configurable timeouts (default 60s)
- Proper retry policies
- Heartbeating for long operations
- Error classification

**Tool Activities**:
- Individual timeout configuration
- Idempotent operations
- Proper serialization of inputs/outputs
- Option to disable activity for deterministic tools

## Observability Patterns

**Specific Observability Implementation Patterns**

Based on existing Temporal integrations like OpenTelemetry:

### 1. Tracing Integration

**Tracing Integration Examples**:

```python
class AgentTracingInterceptor(Interceptor):
    def intercept_activity(self, next, input):
        with tracer.start_span(f"agent.tool.{input.activity_type}"):
            return next(input)
```

**Key Patterns from OpenTelemetry Integration:**
- **Header Propagation**: Pass trace context through workflow/activity boundaries
- **Span Lifecycle**: Create spans for model calls, tool execution, agent runs
- **Baggage Context**: Propagate agent metadata (user_id, session_id) across activities
- **Custom Attributes**: Add agent-specific data (model_name, token_usage, tool_calls)

**OpenAI Agents SDK Implementation:**
- Uses `TemporalTraceProvider` to adapt agent tracing to Temporal spans
- Automatically closes spans in workflows to prevent replay issues
- Propagates trace context to activities via serialized headers

### Conceptual Observability Guide

**Understanding Agent Observability in Temporal**

When integrating observability, think of three layers:

1. **Temporal Layer**: Workflow execution, activity retries, task queue metrics
2. **Agent Layer**: Model calls, tool usage, conversation flow, token consumption
3. **Business Layer**: User sessions, task completion rates, agent performance

**Tracing Flow Example:**
```
User Request → Workflow Span
  ├── Model Call Activity Span (with token usage, latency)
  ├── Tool Execution Activity Span (with success/failure, duration)
  └── Final Response Span (with user satisfaction, task completion)
```

**Key Observability Concepts:**
- **Correlation IDs**: Link user sessions across multiple workflow executions
- **Business Metrics**: Track agent effectiveness, not just technical performance
- **Error Attribution**: Distinguish between model errors, tool failures, and system issues
- **Cost Tracking**: Monitor token usage and API costs per user/session

### 2. Metrics & Monitoring

**Metrics Integration Examples**:

```python
# Activity-level metrics
model_call_duration = histogram("agent_model_call_duration_ms")
model_call_counter = counter("agent_model_calls_total")
tool_execution_errors = counter("agent_tool_errors_total")

# Business metrics
user_sessions = counter("agent_sessions_started")
conversation_turns = histogram("agent_conversation_turns")
```

**Integration Points:**
- **Activity Interceptors**: Wrap activities to emit timing and success/failure metrics
- **Custom Metrics**: Use Temporal's metrics client to emit agent-specific metrics
- **Prometheus Integration**: Export metrics in Prometheus format for monitoring
- **Usage Tracking**: Token consumption, API costs, model performance metrics

## Future Considerations

**Analysis**: These sections provide limited concrete value and should be condensed or removed:

### Valuable Concepts (Keep)
- **Performance Optimizations**: Activity result caching is actually implemented in some scenarios
- **Developer Experience**: Enhanced replay testing and debugging tools are practical improvements

### Remove (Too Speculative)
- Common base classes (premature abstraction)
- Shared serialization utilities (each SDK has different needs)
- Batch tool execution (not a common pattern)
- Connection pooling (handled by underlying HTTP clients)

**Recommendation**: Replace with concrete, actionable guidance based on actual integration experience.

## Integration Procedure

**Step-by-Step Process for Agent SDK Integration:**

### Phase 1: Architecture Design (1-2 weeks)
1. **Analyze SDK**: Identify deterministic vs non-deterministic operations
2. **Design Activities**: Map SDK operations to Temporal activities
3. **Plan Serialization**: Determine data types and serialization strategy
4. **Design Plugin Interface**: Define client and worker plugin responsibilities

### Phase 2: Core Implementation (2-3 weeks)
1. **Create Plugin Classes**: Implement `ClientPlugin` and `WorkerPlugin` 
2. **Implement Activities**: Create activities for model calls and tools
3. **Configure Serialization**: Set up Pydantic payload converters
4. **Handle Errors**: Classify retryable vs non-retryable exceptions

### Phase 3: Integration Features (1-2 weeks)
1. **Workflow Helpers**: Create utilities for common patterns (activity_as_tool)
2. **Sandbox Configuration**: Add required passthrough modules
3. **Observability**: Add tracing and metrics support
4. **Configuration**: Implement flexible activity configuration

### Phase 4: Testing & Documentation (1-2 weeks)
1. **Unit Tests**: Test plugin configuration and activity execution
2. **Replay Tests**: Capture and test workflow histories
3. **Integration Tests**: End-to-end scenarios with real agents
4. **Documentation**: Usage examples, migration guides, troubleshooting

### Phase 5: Samples & Examples (1 week)
1. **Basic Examples**: Hello world, simple tool usage
2. **Advanced Patterns**: Multi-agent workflows, complex orchestration
3. **Migration Examples**: Compare non-Temporal vs Temporal versions

**Total Estimated Time: 5-8 weeks for full-featured integration**

## Testing and Samples Analysis

### Testing Patterns (Based on `/tests/contrib/openai_agents/`)

**Comprehensive Test Structure:**

1. **Unit Tests (`test_openai.py`)**:
   - Mock model providers for deterministic testing
   - Test all major workflow patterns (basic, tools, guardrails, handoffs)
   - Parameterized tests for different agent configurations
   - Error scenario testing (rate limits, API failures)

2. **Replay Tests (`test_openai_replay.py`)**:
   - **Critical for plugin reliability**: Tests workflow history compatibility
   - Stored JSON histories for multiple workflow types
   - Automated replay testing across code versions
   - Pattern: `@pytest.mark.parametrize` with history file names

3. **Tracing Tests (`test_openai_tracing.py`)**:
   - Verify observability integration works correctly
   - Test span creation and context propagation
   - Validate trace data across workflow/activity boundaries

**Key Testing Requirements for Agent SDK Plugins:**
- **Mock Providers**: Create fake/test model providers for consistent testing
- **History Capture**: Save workflow execution histories for replay testing
- **Error Injection**: Test failure scenarios and retry behavior
- **Configuration Testing**: Verify plugin setup and activity registration

### Simple Testing Example for Beginners

**Basic Test Structure for Agent SDK Plugin:**

```python
# test_my_agent_plugin.py
import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

# Your plugin and workflow imports
from my_agent_plugin import MyAgentPlugin
from my_workflows import ChatWorkflow

async def test_simple_agent_workflow():
    """Test that agent responds correctly to simple input"""
    
    # 1. Set up test environment (no real Temporal server needed)
    async with WorkflowEnvironment() as env:
        
        # 2. Create worker with your plugin
        worker = Worker(
            env.client,
            task_queue="test-queue",
            workflows=[ChatWorkflow],
            plugins=[MyAgentPlugin(model="test-model")]  # Use test model
        )
        
        # 3. Start the worker
        async with worker:
            
            # 4. Run workflow and verify result
            result = await env.client.execute_workflow(
                ChatWorkflow.run,
                "Hello, how are you?",
                id="test-workflow",
                task_queue="test-queue"
            )
            
            # 5. Assert expected behavior
            assert "hello" in result.lower()
            assert len(result) > 0

# Key points for beginners:
# - WorkflowEnvironment runs everything in-memory (fast tests)
# - Use mock/test models to avoid API calls
# - Test the workflow logic, not the actual AI model
# - Focus on error handling and edge cases
```

**Why This Testing Approach Works:**
- **No External Dependencies**: Tests run without real Temporal server or AI APIs
- **Deterministic**: Same input always produces same test result
- **Fast**: In-memory execution means tests complete in milliseconds
- **Debuggable**: Easy to step through and understand what's happening

### Sample Patterns (Based on `/samples-python/openai_agents/`)

**Sample Organization Structure:**

1. **Basic Examples** (`/basic/`):
   - `hello_world_workflow.py`: Simplest possible agent workflow
   - `tools_workflow.py`: Agent with tool usage
   - `run_*.py`: Client execution scripts
   - `run_worker.py`: Worker startup script

2. **Advanced Patterns** (`/agent_patterns/`):
   - Multi-agent orchestration
   - Guardrails and validation
   - Agent handoffs and routing
   - Deterministic vs non-deterministic patterns

3. **Real-world Examples** (`/customer_service/`, `/research_bot/`):
   - Complete applications showing production patterns
   - Multi-file organization with separate agent definitions
   - Complex workflow orchestration

**Sample vs Original SDK Examples Comparison:**

| Aspect | Original SDK | Temporal Samples |
|--------|--------------|------------------|
| **Structure** | Single-file scripts | Multi-file workflow organization |
| **Execution** | Direct async/await | Temporal client/worker pattern |
| **State** | In-memory variables | Workflow state management |
| **Errors** | Try/catch blocks | Temporal retry policies |
| **Tools** | Direct function calls | Activities with timeouts |

**Sample Requirements for Agent SDK Plugins:**

1. **Migration Path**: Show identical functionality in original vs. Temporal versions
2. **Gradual Complexity**: Start with hello world, progress to advanced patterns
3. **Production Patterns**: Include realistic multi-agent scenarios
4. **Configuration Examples**: Demonstrate timeout, retry, and error handling
5. **Documentation**: Clear README files explaining each sample's purpose

### Human-in-the-Loop Example

**Scenario**: Customer service agent that escalates complex issues to human agents

```python
# workflow.py - Human-in-the-loop customer service
from temporalio import workflow
from agents import Agent, Runner
from dataclasses import dataclass
from datetime import timedelta

@dataclass
class EscalationRequest:
    customer_message: str
    agent_analysis: str
    urgency: str
    customer_id: str

@workflow.defn
class CustomerServiceWorkflow:
    def __init__(self):
        self.escalated = False
        self.human_response = None
    
    @workflow.run
    async def run(self, customer_message: str, customer_id: str) -> str:
        # 1. Initial AI agent response
        agent = Agent(
            name="CustomerService",
            instructions="Help customers with their issues. If the issue is complex or you're unsure, say 'ESCALATE'."
        )
        
        result = await Runner.run(agent, input=customer_message)
        ai_response = result.final_output
        
        # 2. Check if escalation is needed
        if "ESCALATE" in ai_response.upper():
            # 3. Send to human agent and wait for response
            escalation = EscalationRequest(
                customer_message=customer_message,
                agent_analysis=ai_response,
                urgency="HIGH" if "urgent" in customer_message.lower() else "NORMAL",
                customer_id=customer_id
            )
            
            # 4. Human-in-the-loop: Wait for human agent signal
            await workflow.wait_condition(
                lambda: self.human_response is not None,
                timeout=timedelta(hours=24)  # Max wait time
            )
            
            if self.human_response:
                return self.human_response
            else:
                # Timeout fallback
                return "We're experiencing high volume. A human agent will contact you within 24 hours."
        
        return ai_response
    
    @workflow.signal
    async def human_agent_response(self, response: str):
        """Human agent provides response through this signal"""
        self.human_response = response
    
    @workflow.query
    def get_escalation_status(self) -> dict:
        """Check if case is escalated and waiting for human"""
        return {
            "escalated": "ESCALATE" in getattr(self, '_last_ai_response', ''),
            "waiting_for_human": self.human_response is None,
            "customer_id": getattr(self, '_customer_id', None)
        }

# Usage pattern:
# 1. Customer sends message → Workflow starts
# 2. AI agent processes → Decides to escalate
# 3. Human dashboard shows pending escalation
# 4. Human agent reviews and sends response via signal
# 5. Customer receives human-crafted response
```

**Key Human-in-the-Loop Patterns:**

1. **Signals for Human Input**: Use `@workflow.signal` to receive human responses
2. **Timeouts for SLA**: Set maximum wait times for human response
3. **Queries for Status**: Allow external systems to check escalation status
4. **Graceful Degradation**: Provide fallback responses if humans unavailable
5. **State Management**: Track escalation state across workflow execution

**Benefits of Temporal for Human-in-the-Loop:**
- **Durable Waiting**: Workflow can wait hours/days for human response
- **No Lost Context**: Conversation history preserved during human handoff
- **SLA Management**: Built-in timeout handling ensures customer response
- **Audit Trail**: Complete record of AI→Human→Customer interaction
- **Scalability**: Handle thousands of concurrent escalations

### Recommended Testing Strategy

**For Plugin Authors:**
```python
# 1. Mock model providers
class TestModelProvider(ModelProvider):
    def get_model(self, name: str) -> Model:
        return FakeModel(predetermined_responses)

# 2. Capture histories during development
@pytest.fixture
def capture_history():
    # Run workflow and save history for future replay tests
    
# 3. Test configuration variations
@pytest.mark.parametrize("timeout,retry_policy", [
    (timedelta(seconds=30), RetryPolicy(maximum_attempts=3)),
    # ... other configurations
])
```

**For Sample Creation:**
1. **Start Simple**: Basic "hello world" that works identically to original SDK
2. **Show Migration**: Side-by-side comparison of original vs. Temporal versions
3. **Demonstrate Value**: Examples that showcase Temporal's durability benefits (like human-in-the-loop)
4. **Real Scenarios**: Complex workflows that would be difficult without Temporal

**Human-in-the-Loop as Compelling Example:**
This pattern is particularly compelling because:
- **Impossible without durable execution**: Regular agents can't wait hours for human input
- **Clear business value**: Improves customer satisfaction through seamless escalation
- **Showcases Temporal features**: Signals, queries, timeouts, and long-running workflows
- **Realistic scenario**: Common requirement in customer service, content moderation, and approval workflows

This analysis provides a foundation for creating a comprehensive Agent SDK integration guide that can help developers successfully integrate their Agent SDKs with Temporal's durable execution platform.
