# Google ADK Agents SDK Integration for Temporal

This package provides the integration layer between the Google ADK and Temporal. It allows ADK Agents to run reliably within Temporal Workflows by ensuring determinism and correctly routing external calls (network I/O) through Temporal Activities.

## Core Concepts

### 1. Interception Flow (`AgentPlugin`)

The `AgentPlugin` acts as a middleware that intercepts model calls (e.g., `agent.generate_content`) *before* they execute.

**Workflow Interception:**
1.  **Intercept**: The ADK invokes `before_model_callback` when an agent attempts to call a model.
2.  **Delegate**: The plugin calls `workflow.execute_activity()`, routing the request to Temporal for execution.
3.  **Return**: The plugin awaits the activity result and returns it immediately. The ADK stops its own request processing, using the activity result as the final response.

This ensures that all model interactions are recorded in the Temporal Workflow history, enabling reliable replay and determinism.

### 2. Dynamic Activity Registration

To provide visibility in the Temporal UI, activities are dynamically named after the calling agent (e.g., `MyAgent.generate_content`). Since agent names are not known at startup, the integration uses Temporal's dynamic activity registration.

```python
@activity.defn(dynamic=True)
async def dynamic_activity(args: Sequence[RawValue]) -> Any:
    ...
```

When the workflow executes an activity with an unknown name (e.g., `MyAgent.generate_content`), the worker routes the call to `dynamic_activity`. This handler inspects the `activity_type` and delegates execution to the appropriate internal logic (`_handle_generate_content`), enabling arbitrary activity names without explicit registration.

### 3. Usage & Configuration

The integration requires setup on both the Agent (Workflow) side and the Worker side.

#### Agent Setup (Workflow Side)
Attach the `AgentPlugin` to your ADK agent. This safely routes model calls through Temporal activities. You **must** provide activity options (e.g., timeouts) as there are no defaults.

```python
from datetime import timedelta
from temporalio.common import RetryPolicy
from google.adk.integrations.temporal import AgentPlugin

# 1. Define Temporal Activity Options
activity_options = {
    "start_to_close_timeout": timedelta(minutes=1),
    "retry_policy": RetryPolicy(maximum_attempts=3)
}

# 2. Add Plugin to Agent
agent = Agent(
    model="gemini-2.5-pro",
    plugins=[
        # Routes model calls to Temporal Activities
        AgentPlugin(activity_options=activity_options)  
    ]
)

# 3. Use Agent in Workflow
# When agent.generate_content() is called, it will execute as a Temporal Activity.
```

#### Worker Setup
Install the `WorkerPlugin` on your Temporal Worker. This handles serialization and runtime determinism.

```python
from temporalio.worker import Worker
from google.adk.integrations.temporal import WorkerPlugin

async def main():
    worker = Worker(
        client,
        task_queue="my-queue",
        # Configures ADK Runtime & Pydantic Support
        plugins=[WorkerPlugin()]
    )
    await worker.run()
```

**What `WorkerPlugin` Does:**
*   **Data Converter**: Enables Pydantic serialization for ADK objects.
*   **Interceptors**: Sets up specific ADK runtime hooks for determinism (replacing `time.time`, `uuid.uuid4`) before workflow execution.
*   TODO: is this enough . **Unsandboxed Workflow Runner**: Configures the worker to use the `UnsandboxedWorkflowRunner`, allowing standard imports in ADK agents.
