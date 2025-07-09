# Temporal LangChain Integration

This module provides integration between LangChain and Temporal workflows, allowing you to run LLM models and tools as Temporal activities.

## Features

- **Model Wrappers**: Wrap LangChain models to run as Temporal activities with `model_as_activity()`
- **Tool Wrappers**: Wrap LangChain tools to run as Temporal activities with `tool_as_activity()`
- **Seamless Integration**: Wrapped components maintain the same interface as original LangChain objects
- **Pydantic Support**: Full Pydantic data converter compatibility
- **Configurable**: Flexible activity parameters, timeouts, and retry policies
- **Type Safe**: Full type hints and proper error handling

## Basic Usage

### Wrapping LangChain Components

```python
from temporalio.contrib.langchain import model_as_activity, tool_as_activity
from langchain_openai import ChatOpenAI
from langchain_tavily import TavilySearch

# Wrap a LangChain model to run as a Temporal activity
llm = model_as_activity(ChatOpenAI(model="gpt-4o"))

# Wrap a LangChain tool to run as a Temporal activity  
search_tool = tool_as_activity(TavilySearch(max_results=3))

# Use them exactly like the original LangChain objects!
```

### Using in a Workflow

```python
from temporalio import workflow
from temporalio.contrib.langchain import model_as_activity, tool_as_activity
from langchain_openai import ChatOpenAI
from langchain_tavily import TavilySearch
from langchain.agents import AgentExecutor, create_structured_chat_agent

@workflow.defn  
class SearchWorkflow:
    @workflow.run
    async def run(self, search_query: str) -> str:
        # Wrap LangChain components - they run as activities but maintain the same interface
        llm = model_as_activity(
            ChatOpenAI(model="gpt-4o"),
            start_to_close_timeout=timedelta(minutes=5)
        )
        
        tools = [tool_as_activity(
            TavilySearch(max_results=3),
            start_to_close_timeout=timedelta(minutes=2)
        )]

        # Use standard LangChain patterns - no changes needed!
        agent = create_structured_chat_agent(llm=llm, tools=tools, prompt=prompt)
        agent_executor = AgentExecutor(agent=agent, tools=tools, verbose=True)
        
        result = await agent_executor.ainvoke({"input": search_query})
        return str(result)
```

### Converting Activities to Tools

```python
from temporalio import activity
from temporalio.contrib.langchain import workflow as lc_workflow

@activity.defn
def search_database(query: str) -> str:
    # Your database search logic here
    return f"Found results for: {query}"

@activity.defn
def send_email(recipient: str, subject: str, body: str) -> str:
    # Your email sending logic here
    return f"Email sent to {recipient}"

@workflow.defn
class ToolUsingWorkflow:
    def __init__(self):
        self.model_activity = ModelActivity()
    
    @workflow.run
    async def run(self, user_request: str) -> str:
        # Convert activities to tools
        search_tool = lc_workflow.activity_as_tool(
            search_database,
            start_to_close_timeout=timedelta(seconds=30)
        )
        
        email_tool = lc_workflow.activity_as_tool(
            send_email,
            start_to_close_timeout=timedelta(seconds=10)
        )
        
        messages = [
            {"type": "system", "content": "You can search the database and send emails."},
            {"type": "human", "content": user_request}
        ]
        
        # Use model with tools and auto-execute them
        response = await lc_workflow.invoke_model_with_tools(
            self.model_activity,
            messages,
            available_tools=[search_tool, email_tool],
            temperature=0.7,
            max_iterations=5
        )
        
        return response.content
```

### Worker Setup

```python
import asyncio
from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.langchain import model_as_activity, tool_as_activity, get_wrapper_activities
from langchain_openai import ChatOpenAI
from langchain_tavily import TavilySearch

async def main():
    # Create client with pydantic data converter
    client = await Client.connect(
        "localhost:7233",
        data_converter=pydantic_data_converter
    )
    
    # Pre-create wrapped components to register their activities
    llm = model_as_activity(ChatOpenAI(model="gpt-4o"))
    search_tool = tool_as_activity(TavilySearch(max_results=3))
    
    # Create worker - get_wrapper_activities() returns all registered wrapper activities
    worker = Worker(
        client,
        task_queue="search-task-queue",
        workflows=[SearchWorkflow],
        activities=get_wrapper_activities()  # This gets all wrapper activities
    )
    
    await worker.run()

if __name__ == "__main__":
    asyncio.run(main())
```

## Advanced Configuration

### Custom Activity Parameters

```python
from temporalio.contrib.langchain import ModelActivityParameters
from datetime import timedelta

# Configure activity parameters
activity_params = ModelActivityParameters(
    start_to_close_timeout=timedelta(minutes=5),
    heartbeat_timeout=timedelta(seconds=30),
    retry_policy=RetryPolicy(maximum_attempts=3),
    task_queue="gpu-task-queue"
)

# Use with model invocation
response = await lc_workflow.invoke_model(
    model_activity,
    messages,
    activity_params=activity_params
)
```

### Custom Tool Execution

```python
@workflow.defn
class CustomToolWorkflow:
    def __init__(self):
        self.model_activity = ModelActivity()
    
    @workflow.run
    async def run(self, user_input: str) -> str:
        # Create tool specification
        tool_spec = lc_workflow.activity_as_tool(
            my_activity,
            start_to_close_timeout=timedelta(seconds=30)
        )
        
        # First model call
        messages = [{"type": "human", "content": user_input}]
        response = await lc_workflow.invoke_model(
            self.model_activity,
            messages,
            tools=[tool_spec]
        )
        
        # Handle tool calls manually
        if response.tool_calls:
            for tool_call in response.tool_calls:
                if tool_call["name"] == "my_activity":
                    # Execute the tool
                    result = await tool_spec["execute"](**tool_call["args"])
                    
                    # Continue conversation
                    messages.extend([
                        {"type": "ai", "content": response.content, "tool_calls": response.tool_calls},
                        {"type": "tool", "content": result, "tool_call_id": tool_call["id"]}
                    ])
                    
                    # Get final response
                    final_response = await lc_workflow.invoke_model(
                        self.model_activity,
                        messages
                    )
                    return final_response.content
        
        return response.content
```

## Supported LangChain Models

This integration works with any LangChain model that supports:
- `ainvoke()` method for async invocation
- `bind_tools()` method for tool binding
- Standard LangChain message types

Examples:
- `ChatOpenAI` from `langchain-openai`
- `ChatAnthropic` from `langchain-anthropic`
- `ChatOllama` from `langchain-ollama`
- Any custom LangChain model

## Requirements

- `langchain-core` for base LangChain functionality
- `temporalio` with pydantic data converter
- Your choice of LangChain model provider (e.g., `langchain-openai`)

## Important Notes

### Message History Compatibility

**Update**: `run_in_executor` support has been added to the Temporal workflow event loop, allowing native LangChain message history to work without custom implementations. For maximum compatibility, you can still use the provided `get_temporal_history()` function:

```python
from temporalio.contrib.langchain import get_temporal_history
from langchain_core.runnables.history import RunnableWithMessageHistory

# Instead of the default LangChain history
agent_with_history = RunnableWithMessageHistory(
    agent_executor,
    get_temporal_history,  # Use this instead of custom history function
    input_messages_key="input",
    history_messages_key="chat_history",
)
```

### Workflow Sandbox Limitations

Some LangChain features that use threading or executor services may not work in Temporal workflows. The wrapper functions handle the main execution paths, but complex LangChain chains may need additional compatibility layers.

## Warning

This module is experimental and may change in future versions. Use with caution in production environments.