# Temporal LangChain Integration — Implementation Plan

## 1 Overview
This document describes the technical implementation of the Temporal LangChain integration per the requirements in `SPECIFICATION.md`. The implementation uses two static activities to handle all model and tool invocations, with wrapper classes that maintain LangChain interface compatibility.

---

## 2 Core Architecture

### 2.1 Static Activity Pattern
Two global activities handle all wrapped invocations:
- `langchain_model_call` — executes any LangChain model method
- `langchain_tool_call` — executes any LangChain tool method

Benefits:
- Simplified worker registration (only 2 activities vs. N per wrapper)
- Consistent observability and tracing
- Shared timeout/retry configuration logic

### 2.2 Activity Input/Output Types
```python
from pydantic import BaseModel
from typing import List, Dict, Any
from langchain_core.callbacks import BaseCallbackHandler

class ModelCallInput(BaseModel):
    model_data: bytes  # pickled model object
    model_type: str    # fully qualified class name for validation
    method_name: str   # method to invoke (e.g., "ainvoke")
    args: List[Any]    # positional arguments
    kwargs: Dict[str, Any]  # keyword arguments (callbacks stripped)
    activity_callbacks: List[BaseCallbackHandler]  # callbacks for activity execution

class ToolCallInput(BaseModel):
    tool_data: bytes   # pickled tool object  
    tool_type: str     # fully qualified class name for validation
    method_name: str   # method to invoke (e.g., "ainvoke", "_arun")
    args: List[Any]    # positional arguments
    kwargs: Dict[str, Any]  # keyword arguments (callbacks stripped)
    activity_callbacks: List[BaseCallbackHandler]  # callbacks for activity execution

class CallOutput(BaseModel):
    result: Any        # serialized return value
    callback_events: List[Dict[str, Any]]  # captured callback events for replay

class ModelOutput(BaseModel):
    """Standard output format for LangChain model responses."""
    content: str                                    # Main response content
    tool_calls: Optional[List[Dict[str, Any]]] = None  # Tool calls made by model
    usage_metadata: Optional[Dict[str, Any]] = None    # Token usage and other metadata
    response_metadata: Optional[Dict[str, Any]] = None # Provider-specific metadata
    
    @classmethod
    def from_langchain_response(cls, response: Any) -> "ModelOutput":
        """Convert a LangChain model response to ModelOutput format."""
        # Handle different response types from LangChain models
        if hasattr(response, 'content'):
            # AIMessage or similar
            return cls(
                content=str(response.content),
                tool_calls=getattr(response, 'tool_calls', None),
                usage_metadata=getattr(response, 'usage_metadata', None),
                response_metadata=getattr(response, 'response_metadata', None),
            )
        elif isinstance(response, str):
            # Direct string response
            return cls(content=response)
        else:
            # Fallback - convert to string
            return cls(content=str(response))
```

### 2.3 Module Structure
```
temporalio/contrib/langchain/
├── __init__.py              # Public API exports
├── _activities.py           # Static model_call and tool_call activities
├── _wrappers.py            # TemporalModelWrapper and TemporalToolWrapper
├── _interceptor.py         # LangChain tracing interceptor
├── _callbacks.py           # Callback handling utilities
├── _serialization.py       # Model/tool serialization helpers
├── _registry.py            # Activity registry management
└── _utils.py               # Helper functions
```

---

## 3 Implementation Details

### 3.1 Static Activities (`_activities.py`)

```python
import asyncio
import inspect
import pickle
from typing import Any, Dict, List

from langchain_core.callbacks import BaseCallbackHandler, CallbackManager
from temporalio import activity

@activity.defn(name="langchain_model_call")
async def model_call_activity(input: ModelCallInput) -> CallOutput:
    """Execute a LangChain model method as a Temporal activity."""
    
    # 1. Deserialize and validate model
    model = pickle.loads(input.model_data)
    if type(model).__module__ + "." + type(model).__qualname__ != input.model_type:
        raise ValueError(f"Model type mismatch: expected {input.model_type}")
    
    # 2. Set up callback manager with activity callbacks
    callback_manager = get_callback_manager(input.activity_callbacks)
    
    # 3. Execute method (handle sync via asyncio.to_thread)
    method = getattr(model, input.method_name)
    if inspect.iscoroutinefunction(method):
        result = await method(*input.args, **input.kwargs)
    else:
        result = await asyncio.to_thread(method, *input.args, **input.kwargs)
    
    # 4. Capture callback events for workflow replay
    callback_events = extract_callback_events(callback_manager)
    
    return CallOutput(result=result, callback_events=callback_events)

@activity.defn(name="langchain_tool_call")  
async def tool_call_activity(input: ToolCallInput) -> CallOutput:
    """Execute a LangChain tool method as a Temporal activity."""
    
    # 1. Deserialize and validate tool
    tool = pickle.loads(input.tool_data)
    if type(tool).__module__ + "." + type(tool).__qualname__ != input.tool_type:
        raise ValueError(f"Tool type mismatch: expected {input.tool_type}")
    
    # 2. Set up callback manager with activity callbacks
    callback_manager = get_callback_manager(input.activity_callbacks)
    
    # 3. Execute method (handle sync via asyncio.to_thread)
    method = getattr(tool, input.method_name)
    if inspect.iscoroutinefunction(method):
        result = await method(*input.args, **input.kwargs)
    else:
        # For sync methods like _run, execute via asyncio.to_thread
        result = await asyncio.to_thread(method, *input.args, **input.kwargs)
    
    # 4. Capture callback events for workflow replay
    callback_events = extract_callback_events(callback_manager)
    
    return CallOutput(result=result, callback_events=callback_events)

# Helper functions for callback management
def get_callback_manager(callbacks: List[BaseCallbackHandler]) -> CallbackManager:
    """Create a callback manager with activity callbacks and event capture."""
    from langchain_core.callbacks import CallbackManager
    
    # Add event capture callback to record events for workflow replay
    capture_callback = CallbackEventCapture()
    all_callbacks = callbacks + [capture_callback]
    
    return CallbackManager(all_callbacks)

def extract_callback_events(callback_manager: CallbackManager) -> List[Dict[str, Any]]:
    """Extract captured callback events from the callback manager."""
    # Find the CallbackEventCapture instance
    for callback in callback_manager.handlers:
        if isinstance(callback, CallbackEventCapture):
            return callback.events
    return []
```

### 3.2 Wrapper Classes (`_wrappers.py`)

```python
import asyncio
import pickle
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseLanguageModel
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from temporalio import workflow

class TemporalModelWrapper(BaseLanguageModel):
    """Wrapper that proxies LangChain models to Temporal activities."""
    
    def __init__(self, model: BaseLanguageModel, **activity_params):
        # Validate model is a BaseLanguageModel subclass (FR-1)
        if not isinstance(model, BaseLanguageModel):
            raise ValueError(f"Model must be a subclass of BaseLanguageModel, got {type(model)}")
        
        # Validate model compatibility
        if not (hasattr(model, 'ainvoke') or hasattr(model, 'invoke')):
            raise ValueError("Model must implement ainvoke or invoke")
            
        self._model = model
        self._activity_params = activity_params
        self._workflow_callbacks: List[BaseCallbackHandler] = []
    
    def add_workflow_callback(self, callback: BaseCallbackHandler) -> None:
        """Add a callback to be executed in the workflow thread."""
        self._workflow_callbacks.append(callback)
    
    async def ainvoke(self, input, config: RunnableConfig = None, **kwargs):
        """Main invocation method - runs model in activity."""
        
        # 1. Upsert search attributes before call
        await self._upsert_model_metadata()
        
        # 2. Split callbacks
        activity_callbacks, workflow_callbacks = self._split_callbacks(config)
        
        # 3. Prepare activity input
        activity_input = ModelCallInput(
            model_data=pickle.dumps(self._model),
            model_type=f"{type(self._model).__module__}.{type(self._model).__qualname__}",
            method_name="ainvoke",
            args=[input],
            kwargs={**kwargs, "config": self._strip_callbacks(config)},
            activity_callbacks=activity_callbacks
        )
        
        # 4. Execute activity
        output = await workflow.execute_activity(
            model_call_activity,
            activity_input,
            **self._activity_params
        )
        
        # 5. Replay callback events in workflow
        await self._replay_callbacks(workflow_callbacks, output.callback_events)
        
        return output.result
    
    def __getattr__(self, name: str) -> Any:
        """Forward all other method calls to the wrapped model via activity."""
        attr = getattr(self._model, name)
        
        if callable(attr):
            async def wrapped_method(*args, **kwargs):
                activity_input = ModelCallInput(
                    model_data=pickle.dumps(self._model),
                    model_type=f"{type(self._model).__module__}.{type(self._model).__qualname__}",
                    method_name=name,
                    args=list(args),
                    kwargs=kwargs,
                    activity_callbacks=[]
                )
                
                output = await workflow.execute_activity(
                    model_call_activity,
                    activity_input,
                    **self._activity_params
                )
                return output.result
            return wrapped_method
        
        # For non-callable attributes, return as-is (may be expensive!)
        return attr
    
    async def _upsert_model_metadata(self):
        """Upsert search attributes for model tracking."""
        model_name = getattr(self._model, 'model_name', type(self._model).__name__)
        await workflow.upsert_search_attributes({
            "llm.model_name": model_name
        })
    
    def _split_callbacks(self, config: RunnableConfig) -> Tuple[List, List]:
        """Split callbacks into activity and workflow callbacks."""
        if not config or not config.get('callbacks'):
            return [], self._workflow_callbacks
            
        # For now, all config callbacks go to activity
        # Workflow callbacks come from explicit add_workflow_callback()
        return config['callbacks'], self._workflow_callbacks
    
    def _strip_callbacks(self, config: RunnableConfig) -> RunnableConfig:
        """Remove callbacks from config to avoid sending them to activity."""
        if not config:
            return config
            
        # Create a copy of config without callbacks
        stripped_config = config.copy() if config else {}
        if 'callbacks' in stripped_config:
            del stripped_config['callbacks']
            
        return stripped_config
    
    async def _replay_callbacks(self, callbacks: List[BaseCallbackHandler], events: List[Dict[str, Any]]):
        """Replay callback events in the workflow thread."""
        await WorkflowCallbackReplay.replay_events(callbacks, events)
```

### 3.3 Callback Handling (`_callbacks.py`)

```python
import asyncio
from typing import Any, Dict, List

from langchain_core.callbacks import BaseCallbackHandler

class CallbackEventCapture(BaseCallbackHandler):
    """Captures callback events for replay in workflow.
    
    This handler captures all LangChain callback events during activity execution
    so they can be replayed in the workflow thread for deterministic processing.
    """
    
    def __init__(self):
        self.events = []
    
    # LLM callbacks
    def on_llm_start(self, serialized, prompts, **kwargs):
        self.events.append({
            'event': 'on_llm_start',
            'serialized': serialized,
            'prompts': prompts,
            'kwargs': kwargs
        })
    
    def on_chat_model_start(self, serialized, messages, **kwargs):
        self.events.append({
            'event': 'on_chat_model_start',
            'serialized': serialized,
            'messages': messages,
            'kwargs': kwargs
        })
    
    def on_llm_new_token(self, token, **kwargs):
        self.events.append({
            'event': 'on_llm_new_token',
            'token': token,
            'kwargs': kwargs
        })
    
    def on_llm_end(self, response, **kwargs):
        self.events.append({
            'event': 'on_llm_end', 
            'response': response,
            'kwargs': kwargs
        })
    
    def on_llm_error(self, error, **kwargs):
        self.events.append({
            'event': 'on_llm_error',
            'error': error,
            'kwargs': kwargs
        })
    
    # Chain callbacks
    def on_chain_start(self, serialized, inputs, **kwargs):
        self.events.append({
            'event': 'on_chain_start',
            'serialized': serialized,
            'inputs': inputs,
            'kwargs': kwargs
        })
    
    def on_chain_end(self, outputs, **kwargs):
        self.events.append({
            'event': 'on_chain_end',
            'outputs': outputs,
            'kwargs': kwargs
        })
    
    def on_chain_error(self, error, **kwargs):
        self.events.append({
            'event': 'on_chain_error',
            'error': error,
            'kwargs': kwargs
        })
    
    # Tool callbacks
    def on_tool_start(self, serialized, input_str, **kwargs):
        self.events.append({
            'event': 'on_tool_start',
            'serialized': serialized,
            'input_str': input_str,
            'kwargs': kwargs
        })
    
    def on_tool_end(self, output, **kwargs):
        self.events.append({
            'event': 'on_tool_end',
            'output': output,
            'kwargs': kwargs
        })
    
    def on_tool_error(self, error, **kwargs):
        self.events.append({
            'event': 'on_tool_error',
            'error': error,
            'kwargs': kwargs
        })
    
    # Agent callbacks
    def on_agent_action(self, action, **kwargs):
        self.events.append({
            'event': 'on_agent_action',
            'action': action,
            'kwargs': kwargs
        })
    
    def on_agent_finish(self, finish, **kwargs):
        self.events.append({
            'event': 'on_agent_finish',
            'finish': finish,
            'kwargs': kwargs
        })
    
    # Text callback
    def on_text(self, text, **kwargs):
        self.events.append({
            'event': 'on_text',
            'text': text,
            'kwargs': kwargs
        })

class WorkflowCallbackReplay:
    """Replays callback events in workflow thread."""
    
    @staticmethod
    async def replay_events(callbacks: List[BaseCallbackHandler], events: List[Dict]):
        """Replay captured events through workflow callbacks."""
        for event in events:
            for callback in callbacks:
                method = getattr(callback, event['event'], None)
                if method:
                    # Execute in workflow thread - safe for deterministic operations
                    if asyncio.iscoroutinefunction(method):
                        await method(**event.get('kwargs', {}))
                    else:
                        method(**event.get('kwargs', {}))
```

### 3.4 Tracing Interceptor (`_interceptor.py`)

Based on provided `langchain_interceptor.py`:

```python
import json
from typing import Any, Dict, Mapping, Optional
from opentelemetry import trace
from opentelemetry.trace import SpanContext
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from temporalio import client, worker, converter

class TemporalLangChainTracingInterceptor(client.Interceptor, worker.Interceptor):
    """Interceptor for LangChain tracing context propagation."""
    
    def __init__(self, payload_converter: converter.PayloadConverter = None):
        self._payload_converter = payload_converter or converter.default().payload_converter
    
    def intercept_client(self, next: client.OutboundInterceptor) -> client.OutboundInterceptor:
        return _TracingClientOutboundInterceptor(next, self._payload_converter)
    
    def intercept_activity(self, next: worker.ActivityInboundInterceptor) -> worker.ActivityInboundInterceptor:
        return _TracingActivityInboundInterceptor(next)
    
    def workflow_interceptor_class(self, input: worker.WorkflowInterceptorClassInput):
        return _TracingWorkflowInboundInterceptor

class _TracingClientOutboundInterceptor(client.OutboundInterceptor):
    """Inject OpenTelemetry context into activity headers."""
    
    def __init__(self, next: client.OutboundInterceptor, payload_converter: converter.PayloadConverter):
        super().__init__(next)
        self._payload_converter = payload_converter
    
    async def execute_activity(self, input: client.ExecuteActivityInput) -> Any:
        # Inject current OpenTelemetry context into headers
        current_span = trace.get_current_span()
        if current_span and current_span.get_span_context().is_valid:
            carrier = {}
            TraceContextTextMapPropagator().inject(carrier)
            
            # Add tracing headers to activity
            headers = dict(input.headers or {})
            headers["otel-trace-context"] = json.dumps(carrier)
            input = input._replace(headers=headers)
        
        return await self.next.execute_activity(input)

class _TracingActivityInboundInterceptor(worker.ActivityInboundInterceptor):
    """Extract OpenTelemetry context from activity headers and create child span."""
    
    def __init__(self, next: worker.ActivityInboundInterceptor):
        super().__init__(next)
    
    async def execute_activity(self, input: worker.ExecuteActivityInput) -> Any:
        # Extract OpenTelemetry context from headers
        span_context = None
        if input.headers and "otel-trace-context" in input.headers:
            try:
                carrier = json.loads(input.headers["otel-trace-context"])
                ctx = TraceContextTextMapPropagator().extract(carrier)
                span_context = trace.get_current_span(ctx).get_span_context()
            except Exception:
                pass  # Continue without tracing if extraction fails
        
        # Create child span for activity execution
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span(
            f"langchain_activity_{input.activity.name}",
            context=trace.set_span_in_context(trace.NonRecordingSpan(span_context)) if span_context else None
        ) as span:
            # Add activity metadata to span
            span.set_attribute("temporal.activity.name", input.activity.name)
            span.set_attribute("temporal.activity.type", input.activity.activity_type)
            
            return await self.next.execute_activity(input)

class _TracingWorkflowInboundInterceptor(worker.WorkflowInboundInterceptor):
    """Create workflow spans for LangChain operations."""
    
    def __init__(self, next: worker.WorkflowInboundInterceptor):
        super().__init__(next)
    
    async def execute_workflow(self, input: worker.ExecuteWorkflowInput) -> Any:
        # Create root span for workflow execution
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span(f"langchain_workflow_{input.workflow.name}") as span:
            span.set_attribute("temporal.workflow.name", input.workflow.name)
            span.set_attribute("temporal.workflow.type", input.workflow.workflow_type)
            
            return await self.next.execute_workflow(input)
```

### 3.5 Tool Wrapper (`_wrappers.py` continued)

```python
class TemporalToolWrapper(BaseTool):
    """Wrapper that proxies LangChain tools to Temporal activities."""
    
    def __init__(self, tool: BaseTool, **activity_params):
        self._tool = tool
        self._activity_params = activity_params
        self._workflow_callbacks: List[BaseCallbackHandler] = []
        
        # Initialize BaseTool with tool properties
        super().__init__(
            name=tool.name,
            description=tool.description,
            args_schema=getattr(tool, 'args_schema', None),
            return_direct=getattr(tool, 'return_direct', False),
            verbose=getattr(tool, 'verbose', False),
        )
    
    def add_workflow_callback(self, callback: BaseCallbackHandler) -> None:
        """Add a callback to be executed in the workflow thread."""
        self._workflow_callbacks.append(callback)
    
    async def _arun(self, *args, **kwargs) -> str:
        """Async run method - delegates to activity."""
        # Determine which method to call - prefer _arun, fallback to _run
        method_name = "_arun" if hasattr(self._tool, '_arun') else "_run"
        
        activity_input = ToolCallInput(
            tool_data=pickle.dumps(self._tool),
            tool_type=f"{type(self._tool).__module__}.{type(self._tool).__qualname__}",
            method_name=method_name, 
            args=list(args),
            kwargs=kwargs,
            activity_callbacks=[]
        )
        
        output = await workflow.execute_activity(
            tool_call_activity,
            activity_input,
            **self._activity_params
        )
        return str(output.result)
    
    def _run(self, *args, **kwargs) -> str:
        """Synchronous run method - not directly usable in workflow context."""
        # In Temporal workflows, we cannot make blocking calls
        # This method exists for compatibility but should not be called directly
        raise NotImplementedError(
            "Synchronous _run method cannot be called from workflow context. "
            "LangChain agents should use the async _arun method instead. "
            "The underlying tool's sync method will be executed via asyncio.to_thread "
            "in the activity implementation."
        )
    
    # Override invoke to delegate to _arun for better compatibility
    async def ainvoke(self, input: str, config: Optional[Dict] = None, **kwargs) -> str:
        """Async invoke method - delegates to _arun."""
        return await self._arun(input, **kwargs)

# Note on Sync vs Async Tool Handling:
# 
# 1. LangChain tools wrapped with tool_as_activity():
#    - Both sync and async tools are supported
#    - Sync tools are executed via asyncio.to_thread in the activity
#    - The _arun method intelligently detects and handles both cases
#
# 2. Temporal activities exposed via workflow.activity_as_tool():
#    - Must be async since they're executed via workflow.execute_activity()
#    - This is a Temporal limitation, not a LangChain limitation
#    - Activities inherently return awaitable results
#
# 3. Tool compatibility:
#    - LangChain → Temporal: ✅ Both sync and async tools work
#    - Temporal → LangChain: ⚠️ Only async (activities are inherently async)
```

### 3.6 Workflow-scoped Activity-as-Tool (`_utils.py`)

Following OpenAI integration patterns with workflow-scoped method:

```python
import inspect
from typing import Any, Callable, Dict, Optional, Type
from datetime import timedelta
from pydantic import BaseModel, create_model
from temporalio import activity, workflow
from temporalio.common import Priority, RetryPolicy
from temporalio.workflow import ActivityCancellationType, VersioningIntent
from temporalio.exceptions import ApplicationError

class workflow:
    """Workflow-scoped utilities for LangChain integration."""
    
    @classmethod
    def activity_as_tool(
        cls,
        fn: Callable,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        task_queue: Optional[str] = None,
        schedule_to_close_timeout: Optional[timedelta] = None,
        schedule_to_start_timeout: Optional[timedelta] = None,
        start_to_close_timeout: Optional[timedelta] = None,
        heartbeat_timeout: Optional[timedelta] = None,
        retry_policy: Optional[RetryPolicy] = None,
        cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
        activity_id: Optional[str] = None,
        versioning_intent: Optional[VersioningIntent] = None,
        summary: Optional[str] = None,
        priority: Priority = Priority.default,
    ) -> Dict[str, Any]:
        """Convert a Temporal activity to a LangChain tool specification.
        
        This method converts a Temporal activity function into a tool specification
        that can be used with LangChain models. The tool will automatically handle
        the execution of the activity during workflow execution.
        
        Args:
            fn: A Temporal activity function to convert to a tool.
            name: Optional name override for the tool.
            description: Optional description for the tool.
            **activity_params: Standard Temporal activity execution parameters.
            
        Returns:
            A dictionary containing the tool specification for LangChain.
            
        Raises:
            ApplicationError: If the function is not properly decorated as a Temporal activity.
        """
        # Check if function is a Temporal activity
        activity_defn = activity._Definition.from_callable(fn)
        if not activity_defn:
            raise ApplicationError(
                "Function must be decorated with @activity.defn",
                "invalid_activity",
            )
        
        # Extract metadata
        tool_name = name or activity_defn.name or fn.__name__
        tool_description = description or fn.__doc__ or f"Execute {tool_name} activity"
        
        # Generate schema from function signature
        sig = inspect.signature(fn)
        args_schema = _generate_pydantic_schema(sig, tool_name)
        
        async def execute(**kwargs) -> str:
            """Execute the activity with given arguments."""
            # Convert kwargs to positional args in the correct order
            args = []
            for param_name in sig.parameters.keys():
                if param_name in kwargs:
                    args.append(kwargs[param_name])
            
            # Execute the activity
            result = await workflow.execute_activity(
                fn,
                args=args,
                task_queue=task_queue,
                schedule_to_close_timeout=schedule_to_close_timeout,
                schedule_to_start_timeout=schedule_to_start_timeout,
                start_to_close_timeout=start_to_close_timeout,
                heartbeat_timeout=heartbeat_timeout,
                retry_policy=retry_policy,
                cancellation_type=cancellation_type,
                activity_id=activity_id,
                versioning_intent=versioning_intent,
                summary=summary,
                priority=priority,
            )
            
            # Convert result to string for LangChain
            return str(result)
        
        return {
            "name": tool_name,
            "description": tool_description,
            "args_schema": args_schema,
            "execute": execute
        }

def _generate_pydantic_schema(sig: inspect.Signature, tool_name: str) -> Type[BaseModel]:
    """Generate Pydantic schema from function signature."""
    from typing import get_type_hints
    
    # Get type hints for the function
    try:
        type_hints = get_type_hints(sig.func) if hasattr(sig, 'func') else {}
    except:
        type_hints = {}
    
    fields = {}
    for param_name, param in sig.parameters.items():
        param_type = type_hints.get(param_name, param.annotation)
        if param_type == inspect.Parameter.empty:
            param_type = str  # Default to string
        
        default = param.default if param.default != inspect.Parameter.empty else ...
        fields[param_name] = (param_type, default)
    
    if fields:
        return create_model(f"{tool_name.title()}Args", **fields)
    else:
        # Return a minimal schema for tools with no arguments
        return create_model(f"{tool_name.title()}Args")
```

---

## 4 Public API Implementation

### 4.1 Main Factory Functions (`__init__.py`)

```python
from typing import Callable, List, Optional

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseLanguageModel
from langchain_core.tools import BaseTool

def model_as_activity(
    model: BaseLanguageModel,
    workflow_callbacks: List[BaseCallbackHandler] = None,
    **activity_params
) -> TemporalModelWrapper:
    """Wrap a LangChain model as a Temporal activity."""
    wrapper = TemporalModelWrapper(model, **activity_params)
    
    if workflow_callbacks:
        for callback in workflow_callbacks:
            wrapper.add_workflow_callback(callback)
    
    return wrapper

def tool_as_activity(
    tool: BaseTool,
    workflow_callbacks: List[BaseCallbackHandler] = None,
    **activity_params  
) -> TemporalToolWrapper:
    """Wrap a LangChain tool as a Temporal activity."""
    wrapper = TemporalToolWrapper(tool, **activity_params)
    
    if workflow_callbacks:
        for callback in workflow_callbacks:
            wrapper.add_workflow_callback(callback)
    
    return wrapper

def get_wrapper_activities() -> List[Callable]:
    """Return static activities for worker registration."""
    return [model_call_activity, tool_call_activity]
```

---

## 5 Error Handling & Edge Cases

### 5.1 Serialization Issues
- **Large models**: Implement size limits and fallback to model registration
- **Unpicklable objects**: Clear error messages with suggestions
- **Version mismatches**: Type validation prevents runtime errors

### 5.2 Callback Complexity
- **Stateful callbacks**: Document limitations, suggest alternatives
- **I/O callbacks**: Clear separation between activity and workflow callbacks
- **Error propagation**: Ensure callback errors don't break activity execution

### 5.3 Model Property Access
- **Expensive properties**: Document which properties trigger activities
- **Caching strategy**: Consider wrapper-side caching for frequently accessed properties

---

## 6 Testing Strategy

### 6.1 Unit Tests
- **Wrapper functionality**: All public methods proxied correctly
- **Callback splitting**: Proper separation and replay
- **Serialization**: Round-trip model/tool serialization
- **Activity execution**: Mock activity calls and verify inputs

### 6.2 Integration Tests  
- **Real models**: OpenAI, Anthropic, local models
- **Tool execution**: Common LangChain tools (search, calculator)
- **Agent workflows**: Full AgentExecutor scenarios
- **Tracing**: End-to-end trace propagation

### 6.3 Performance Tests
- **Serialization overhead**: Large model serialization time
- **Activity latency**: Network round-trip measurements
- **Memory usage**: Wrapper memory footprint

---

## 7 Migration & Compatibility

### 7.1 Backward Compatibility
- Maintain existing LangChain interfaces exactly
- No breaking changes to method signatures
- Error messages guide users to correct usage

### 7.2 Version Support
- LangChain-core ≥ 0.1.0 compatibility matrix
- Test against multiple LangChain versions
- Clear documentation of supported model providers

---

## 8 Implementation Phases

### Phase 1: Core Infrastructure
- Static activities with basic serialization
- Simple wrapper classes (no callbacks)
- Activity registry and factory functions

### Phase 2: Callback System
- Callback splitting implementation
- Event capture and replay
- Workflow callback support

### Phase 3: Advanced Features
- Tracing interceptor
- Search attribute upserts
- activity_as_tool implementation

### Phase 4: Polish & Testing
- Comprehensive test suite
- Performance optimization
- Documentation and examples

---

*End of implementation plan.* 