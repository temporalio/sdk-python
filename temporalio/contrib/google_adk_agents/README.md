# Google ADK Agents SDK Integration for Temporal

This package provides the integration layer between the Google ADK and Temporal. It allows ADK Agents to run reliably within Temporal Workflows by ensuring determinism and correctly routing external calls (network I/O) through Temporal Activities.

## What's Included

### Core ADK Integration
- **`AdkAgentPlugin`**: Intercepts model calls and executes them as Temporal activities
- **`TemporalAdkPlugin`**: Worker plugin that configures runtime determinism and Pydantic serialization
- **`invoke_model`**: Activity for executing LLM model calls with proper error handling

### MCP (Model Context Protocol) Integration  
- **`TemporalMcpToolSet`**: Executes MCP tools as Temporal activities
- **`TemporalMcpToolSetProvider`**: Manages toolset creation and activity registration
- Full support for tool confirmation and event actions within workflows

### OpenTelemetry Integration
- Automatic instrumentation for ADK components when exporters are provided
- Tracing integration that works within Temporal's execution context
- Support for custom span exporters

### Key Features

#### 1. Deterministic Runtime
- Replaces `time.time()` with `workflow.now()` when in workflow context
- Replaces `uuid.uuid4()` with `workflow.uuid4()` for deterministic IDs
- Automatic setup when using `TemporalAdkPlugin`

#### 2. Activity-Based Model Execution
Model calls are intercepted and executed as Temporal activities with configurable:
- Timeouts (schedule-to-close, start-to-close, heartbeat)
- Retry policies
- Task queues
- Cancellation behavior
- Priority levels

#### 3. Sandbox Compatibility
- Automatic passthrough for `google.adk`, `google.genai`, and `mcp` modules
- Works with both sandboxed and unsandboxed workflow runners

#### 4. Advanced Serialization
- Pydantic payload converter for ADK objects
- Proper handling of complex ADK data types
- Maintains type safety across workflow boundaries

## Usage

### Basic Setup

**Agent (Workflow) Side:**
```python
from temporalio.contrib.google_adk_agents import AdkAgentPlugin, ModelActivityParameters
from datetime import timedelta

# Configure activity parameters
activity_params = ModelActivityParameters(
    start_to_close_timeout=timedelta(minutes=1),
    retry_policy=RetryPolicy(maximum_attempts=3)
)

# Add to agent
agent = Agent(
    model="gemini-2.5-pro", 
    plugins=[AdkAgentPlugin(activity_params)]
)
```

**Worker Side:**
```python
from temporalio.contrib.google_adk_agents import TemporalAdkPlugin

worker = Worker(
    client,
    task_queue="my-queue",
    plugins=[TemporalAdkPlugin()]
)
```

### Advanced Features

**With MCP Tools:**
```python
from temporalio.contrib.google_adk_agents import (
    TemporalAdkPlugin, 
    TemporalMcpToolSetProvider,
    TemporalMcpToolSet
)

# Create toolset provider
provider = TemporalMcpToolSetProvider("my-tools", my_toolset_factory)

# Use in agent workflow
agent = Agent(
    model="gemini-2.5-pro",
    toolsets=[TemporalMcpToolSet("my-tools")]
)

# Configure worker
worker = Worker(
    client,
    plugins=[TemporalAdkPlugin(toolset_providers=[provider])]
)
```

**With OpenTelemetry:**
```python
from opentelemetry.exporter.jaeger.thrift import JaegerExporter

exporter = JaegerExporter(endpoint="http://localhost:14268/api/traces")
plugin = TemporalAdkPlugin(otel_exporters=[exporter])
```

## Integration Points

This integration provides comprehensive support for running Google ADK Agents within Temporal workflows while maintaining:
- **Determinism**: All non-deterministic operations are routed through Temporal
- **Observability**: Full tracing and activity visibility
- **Reliability**: Proper retry handling and error propagation  
- **Extensibility**: Support for custom tools via MCP protocol
