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
- Agent orchestration logic
- Decision making
- State management
- Control flow
**@claude**: please elaborate on all of the above, especially decision making

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

**@claude** can you analyze and determine whether the TemporalAgent wrapper was necessary here? Make it clear that, stylistically, we want users to write Temporal workflows. The abstractions can hide some of the activity creation.

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

**@claude** note that OpenAI Agents SDK takes inputs as TypedDicts and produces outputs at Pydantic models. This adds some complexity to our implementation.

### 2. Context Management

**Context Limitations in Activities**:
- Activities receive limited context (only serializable parts)
- Model, usage, prompt, messages not available in activities
- Custom serialization methods for extended context

**@claude**: explain what context is in openai agents sdk. Does Pydantic have this too?

**Pydantic AI Example**:
```python
class TemporalRunContext(RunContext[AgentDepsT]):
    @classmethod
    def serialize_run_context(cls, ctx: RunContext[AgentDepsT]) -> dict[str, Any]:
        # Only serialize specific fields for activity use
```

**@claude**: can you elaborate on this example - what do we see here. why did you include it?

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

**@claude**: in openai agents sdk we took care to give Temporal control over the retry policy. The exceptions that get thrown by the activity contain the underlying http exception, giving us access to error codes and HTTP headers that suggest how to retry. validate this and document it.

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
**@claude**: explain and give context. how will a library author determine what to put here.


## Design Trade-offs

### OpenAI Agents SDK vs Pydantic AI

**@claude**: let's be clear that we prefer the OpenAI Agents SDK style.

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

**@claude**: for now do not provide a guide to streaming. This will be added later.

**Challenge**: Activities can't stream directly to workflow
**Solutions**:
- Event stream handlers (Pydantic AI)
- External message queues
- Dependency injection for connection details

### 2. Tool Context Updates

**Challenge**: Activity tools can't update workflow context
**Solutions**:
- Read-only context in activities
- Return context updates as activity results
- Use workflow state for context management

**@claude**: do the implementations actually return context updates as activity results?

### 3. Serialization Limits

**Challenge**: 2MB payload limit for Temporal
**Solutions**:
- Minimal context serialization
- External storage for large payloads
- Compression for large data

**@claude**: is this part of the integration? We have a "claim check" pattern that uses other storage.

### 4. Replay Testing

**Challenge**: Ensuring deterministic behavior across versions
**Solutions**:
- Comprehensive replay test suites
- Patching for non-deterministic changes
- Version-aware activity naming

**@claude**: this is worthy of discussion / disclaimer. What does the agent SDK plugin author need to be aware of?

## Integration Requirements

### 1. Agent SDK Requirements

For successful Temporal integration, Agent SDKs need:
- **Stable Naming**: Agent and tool names must be stable for production
- **Serializable State**: All passed data must be Pydantic-serializable
- **Deterministic Separation**: Clear boundaries between deterministic and non-deterministic operations
- **Error Classification**: Distinguish retryable vs non-retryable errors

**@claude**: think and make sure this is correct

### 2. Plugin Architecture Requirements

**Client Plugin Must Provide**:
- Custom data converter configuration
- Serialization setup for agent types

**@claude**: anything else?

**Worker Plugin Must Provide**:
- Activity registration
- Interceptor configuration
- Workflow sandbox configuration
- Error handling setup

**@claude**: anything else?

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

### 1. Tracing Integration

**Temporal-Aware Tracing**:
- Custom trace providers
- Activity-aware span management
- Workflow context propagation

### 2. Metrics & Monitoring

**Plugin Integration Points**:
- Activity execution metrics
- Model call telemetry
- Tool usage statistics
- Error rate monitoring

## Future Considerations

**@claude**: look at these carefully and see whether there is really value here. if not, cut them.

### 1. Standardization Opportunities

- Common base classes for agent plugins
- Standardized activity configuration patterns
- Shared serialization utilities
- Common error handling patterns

### 2. Performance Optimizations

- Activity result caching
- Batch tool execution
- Optimized serialization
- Connection pooling


### 3. Developer Experience

- Better debugging tools
- Enhanced replay testing
- Configuration validation
- Documentation templates

## Recommendations for New Integrations

1. **Start with Plugin Architecture**: Use both client and worker plugins
2. **Follow Activity Patterns**: Separate deterministic orchestration from non-deterministic operations
3. **Handle Serialization Early**: Plan for Pydantic-based serialization from the start
4. **Design for Configuration**: Provide flexible activity configuration options
5. **Plan for Observability**: Include tracing and metrics from initial design
6. **Test Thoroughly**: Include replay testing and error scenarios
7. **Document Clearly**: Provide examples and migration guides

This analysis provides a foundation for creating a comprehensive Agent SDK integration guide that can help developers successfully integrate their Agent SDKs with Temporal's durable execution platform.

**@claude**: can we replace this with an outline of the procedure?