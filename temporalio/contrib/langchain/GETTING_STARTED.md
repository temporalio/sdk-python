# Getting Started with Temporal LangChain Integration

Get up and running with durable AI workflows in 5 minutes.

## 🚀 Quick Setup

### 1. Install Dependencies
```bash
pip install temporalio[langchain,pydantic]
pip install langchain-openai  # or your preferred provider
```

### 2. Start Temporal Server
```bash
# Using Temporal CLI (recommended)
temporal server start-dev

# Or using Docker
docker run -p 7233:7233 -p 8233:8233 temporalio/auto-setup:latest
```

### 3. Your First AI Workflow

```python
# main.py
import asyncio
from datetime import timedelta
from temporalio import workflow, activity
from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.contrib.langchain import model_as_activity, get_wrapper_activities

# Optional: Use real AI model (requires OPENAI_API_KEY)
try:
    from langchain_openai import ChatOpenAI
    model = ChatOpenAI(model="gpt-4o-mini")
except ImportError:
    # Fallback to mock model
    class MockModel:
        async def ainvoke(self, input, **kwargs):
            class Response:
                content = f"AI response to: {input}"
            return Response()
    model = MockModel()

@workflow.defn
class AIWorkflow:
    @workflow.run
    async def run(self, user_question: str) -> str:
        # Wrap LangChain model as Temporal activity
        ai_model = model_as_activity(
            model,
            start_to_close_timeout=timedelta(seconds=30)
        )
        
        # Use it exactly like a normal LangChain model
        response = await ai_model.ainvoke(user_question)
        return response.content

async def main():
    # Connect to Temporal
    client = await Client.connect("localhost:7233")
    
    # Start worker in background
    worker = Worker(
        client,
        task_queue="ai-queue",
        workflows=[AIWorkflow],
        activities=get_wrapper_activities()  # Register LangChain activities
    )
    
    async with worker:
        # Run workflow
        result = await client.execute_workflow(
            AIWorkflow.run,
            "What is the capital of France?",
            id="ai-workflow-1",
            task_queue="ai-queue"
        )
        print(f"AI Response: {result}")

if __name__ == "__main__":
    asyncio.run(main())
```

### 4. Run It
```bash
python main.py
```

## 🎯 What Just Happened?

1. **Durable AI**: Your AI model calls are now durable - they'll retry on failures and survive process restarts
2. **Observability**: Check the Temporal Web UI at http://localhost:8233 to see your workflow execution
3. **Scalability**: Multiple workers can process AI workflows in parallel
4. **Reliability**: Built-in timeouts, retries, and error handling

## 🔧 Next Steps

### Add Tools
```python
from temporalio.contrib.langchain import tool_as_activity, workflow as lc_workflow

# Wrap LangChain tools
weather_tool = tool_as_activity(your_weather_tool)

# Or convert Temporal activities to tools
@activity.defn
async def search_database(query: str) -> str:
    return f"Database results for: {query}"

@workflow.defn
class AgentWorkflow:
    @workflow.run
    async def run(self, request: str) -> str:
        # Convert activity to LangChain tool
        db_tool = lc_workflow.activity_as_tool(search_database)
        
        # AI can now call your database
        ai_model = model_as_activity(ChatOpenAI())
        response = await ai_model.ainvoke(request, tools=[db_tool])
        return response.content
```

### Configure for Production
```python
from temporalio.common import RetryPolicy

ai_model = model_as_activity(
    ChatOpenAI(model="gpt-4"),
    # Production configuration
    start_to_close_timeout=timedelta(minutes=5),
    retry_policy=RetryPolicy(
        initial_interval=timedelta(seconds=1),
        maximum_interval=timedelta(seconds=30),
        maximum_attempts=3
    ),
    task_queue="gpu-workers"  # Route to GPU-enabled machines
)
```

### Monitor and Observe
```python
@workflow.defn
class MonitoredWorkflow:
    @workflow.run
    async def run(self, input: str) -> str:
        # Automatic search attributes for filtering
        ai_model = model_as_activity(ChatOpenAI(model="gpt-4"))
        response = await ai_model.ainvoke(input)
        
        # Query in Temporal Web: llm.model_name = "gpt-4"
        return response.content
```

## 📚 Learn More

- [Complete Examples](./example_comprehensive.py) - Full AI agent with tools
- [API Reference](./README.md) - Detailed documentation
- [Testing Guide](../../../tests/contrib/langchain/) - How to test your workflows

## 🆘 Troubleshooting

**Connection Issues**: Make sure Temporal server is running on `localhost:7233`  
**Import Errors**: Install missing dependencies: `pip install temporalio[langchain,pydantic]`  
**Timeout Issues**: Increase `start_to_close_timeout` for slow models  
**Serialization Errors**: Use smaller models or implement model registration pattern  

---

**🎉 Welcome to durable AI workflows!** Your AI applications can now handle failures gracefully, scale horizontally, and provide complete observability. 