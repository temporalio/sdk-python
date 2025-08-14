# Plugging into Workflows - Python Integrator’s Guide

Prerequisite: You should know basics about Temporal before using this documentation. Check out [learn.temporal.io](http://learn.temporal.io/) for content.  And here is a briefer introduction to [Workflows and Activities](https://docs.temporal.io/evaluate/understanding-temporal#temporal-application-the-building-blocks), the two most critical building blocks.

# What’s a Plugin?

A **Plugin** is an abstraction that will let your users add your feature to their workflows in one line.  There are two types of Plugins, and yours can mix in one or both.

- Worker Plugins contain functionality that runs inside your users’ Workflows.
- Client Plugins contain functionality that runs when Workflows are created and return results.

# Should I build a plugin?

Plugging in into Workflows makes it seamless for Temporal developers to add your functionality to their Workflows.

Here are some common use cases for plugins:

- AI Agent SDKs.
- Observability, tracing, or logging middleware.
- Adding reliable built-in functionality such as LLM calls, corporate messaging, and payments infrastructure.
- Encryption or compliance middleware.

# What are common recipes for AI?

See [AI Recipes](https://www.notion.so/AI-Recipes-2488fc5677388064a01fd88f63af5340?pvs=21) 

# What can I provide my users in a plugin?

- [Built-in Activities](https://www.notion.so/Plugging-into-Workflows-Python-Integrator-s-Guide-2488fc56773880ebbafce42fe97b0c87?pvs=21)
- [Workflow libraries](https://www.notion.so/Plugging-into-Workflows-Python-Integrator-s-Guide-2488fc56773880ebbafce42fe97b0c87?pvs=21)
- [Built-in Child Workflows](https://www.notion.so/Plugging-into-Workflows-Python-Integrator-s-Guide-2488fc56773880ebbafce42fe97b0c87?pvs=21)
- [Built-in Nexus Operations](https://www.notion.so/Plugging-into-Workflows-Python-Integrator-s-Guide-2488fc56773880ebbafce42fe97b0c87?pvs=21)
- [Custom Data Converters](https://www.notion.so/Plugging-into-Workflows-Python-Integrator-s-Guide-2488fc56773880ebbafce42fe97b0c87?pvs=21)
- [Interceptors](https://www.notion.so/Plugging-into-Workflows-Python-Integrator-s-Guide-2488fc56773880ebbafce42fe97b0c87?pvs=21)

# What functionality would I provide in an Activity?

You can provide built-in Activities in a Plugin for users to call from their workflows.

Activities are the most common Temporal primitive and should be fairly fine-grained:

- A single write operation
- A batch of similar writes
- One or more read operations followed by a write operation
- An read that should be memoized, such as an LLM call, a large download, or a slow polling read

Larger bits of functionality should be broken it up into multiple activities.

Here are the most important pieces of advice:

- Activity arguments and return values must be serializable.
- Activities have [timeouts](https://docs.temporal.io/develop/python/failure-detection#heartbeat-timeout) and [retry policies](https://docs.temporal.io/encyclopedia/retry-policies).  To be Activity-friendly, your operation should either complete within a few minutes, or it should support the ability to heartbeat or poll for a result.  This way, it will be clear to the Workflow when the activity is still making progress.
- Activities that perform writes should should be idempotent.

## How to add a builtin Activity to a Plugin in Python

```python
@activity.defn
async def some_activity() -> None:
		return None

class Plugin(temporalio.worker.Plugin):
    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        config["activities"] = list(config.get("activities") or []) + [some_activity]
        return self.next_worker_plugin.configure_worker(config)
```

# What functionality would I provide in a Workflow library?

You can provide a library of functionality for use within a Workflow.  Your library will call elements that you include in your Plugin—Activities, Child Workflows, Signals, Updates, and Queries, Nexus Operations, Interceptors, and Data Converters, as well as any other code as long as it obeys certain properties:

- It should be [deterministic](https://docs.temporal.io/workflows#deterministic-constraints), running the same way every time it’s executed.  Non-deterministic code should go in Activities or Nexus Operations.
- It should be passed through the python [sandbox](https://docs.temporal.io/develop/python/python-sdk-sandbox).
- It should be re-entrant; that is, you cannot assume that the process where a Workflow begins will be the same process when it completes.
- It should run quickly—it may run many times during a long Workflow execution.

A Plugin author’s goal should be to allow their users to decompose their Workflows into Activities, as well as sometimes Child Workflows and Nexus Calls.  This gives users granular robustness via retries and timeouts, debuggability via Temporal UI, operability with resets, pauses, and cancels, memoization for efficiency and resumability, and scalability using task queues and Workers.

Users use Workflows for:

- Orchestration and decision-making
- Interactivity via [message-passing](https://docs.temporal.io/evaluate/development-production-features/workflow-message-passing)
- Tracing and observability.

## Making changes to your Workflow Library

Your users may want to keep their Workflows running across deploys of their code.  If their deployment includes a new version of your Plugin, changes your Plugin could break Workflow code that start before the new version was deployed.  That is, [code changes can cause non-deterministic behavior](https://docs.temporal.io/workflow-definition#non-deterministic-change).

Therefore, as you make changes, you’ll want to use [patching](https://docs.temporal.io/patching) and [replay testing](https://docs.temporal.io/develop/python/testing-suite#replay) to make sure that you’re not causing non-determinism errors (NDEs) for your users.

## How to create a Workflow Library that uses a Plugin in Python

- See [https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)
- For testing, see: [https://github.com/temporalio/sdk-python/tree/main/tests/contrib/openai_agents](https://github.com/temporalio/sdk-python/tree/main/tests/contrib/openai_agents)
In particular [https://github.com/temporalio/sdk-python/blob/main/tests/contrib/openai_agents/test_openai_replay.py](https://github.com/temporalio/sdk-python/blob/main/tests/contrib/openai_agents/test_openai_replay.py) for replay testing.

# What functionality would I provide in in a Child Workflow?

You can provide a built-in workflow in a Plugin.  It’s callable as a child workflow or standalone.

When you want to provide a piece of functionality more sophisticated than an Activity, you can use a [Workflow Library](https://www.notion.so/Plugging-into-Workflows-Python-Integrator-s-Guide-2488fc56773880ebbafce42fe97b0c87?pvs=21) or a Child Workflow.  Do the latter when:

- The child needs its own separate Workflow ID.
- That child should outlive the parent.
- The Workflow Event History would otherwise [not scale](https://docs.temporal.io/workflow-execution/event#event-history-limits) in the parent.

## How to add a builtin Workflow to a Plugin in Python

```python
@workflow.defn
class HelloWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return f"Hello, {name}!"

class Plugin(temporalio.worker.Plugin):
    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        config["workflows"] = list(config.get("workflows") or []) + [HelloWorkflow]
        return self.next_worker_plugin.configure_worker(config)
 
...

client = await Client.connect(
    "localhost:7233",
    plugins=[
        Plugin(),
    ],
)
async with Worker(
    client,
    task_queue="task-queue",
):
    client.execute_workflow(
				HelloWorkflow.run,
				"Tim",
				task_queue=worker.task_queue,
		)    
    
```

# What functionality would I provide in a Nexus Operation?

Nexus calls are used from Workflows similarly to Activities; see [Nexus Use Cases](https://docs.temporal.io/nexus/use-cases).

Like Activities, Nexus Calls’ arguments and return values must be serializable.

## How to add a builtin Nexus Operation to a Plugin in Python

```python

@nexusrpc.service
class WeatherService:
    get_weather_nexus_operation: nexusrpc.Operation[WeatherInput, Weather]

@nexusrpc.handler.service_handler(service=WeatherService)
class WeatherServiceHandler:
    @nexusrpc.handler.sync_operation
    async def get_weather_nexus_operation(
        self, ctx: nexusrpc.handler.StartOperationContext, input: WeatherInput
    ) -> Weather:
        return Weather(
            city=input.city, temperature_range="14-20C", conditions="Sunny with wind."
        )

class Plugin(temporalio.worker.Plugin):
    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        config["nexus_service_handlers"] = list(config.get("nexus_service_handlers") or []) + [WeatherServiceHandler()]
        return self.next_worker_plugin.configure_worker(config)
```

# What functionality would I provide in a Data Converter?

A [Custom Data Converter](https://docs.temporal.io/default-custom-data-converters#custom-data-converter) can alter data formats or provide compression or encryption.

Note that you can use an existing Data Converter such as `PydanticPayloadConverter` in your plugin.

## How to add a Custom Data Converter to a Plugin in Python

```python
class Plugin(temporalio.worker.Plugin):
    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        if config["data_converter"] == temporalio.converter.DataConverter.default:
		        config["data_converter"] = pydantic_data_converter
		    else:
		    	  # Should consider interactions with other plugins, 
		    	  # as this will override the data converter

        return self.next_worker_plugin.configure_worker(config)
```

# What functionality would I provide in an interceptor?

[Interceptors](https://docs.temporal.io/develop/python/interceptors) are middleware that can run before you call an activity or nexus operation and after it returns a result.  They are used to:

- Create side effects such as logging and tracing.
- Modify arguments, such as adding headers for authorization or tracing propagation.

One advantage of using interceptors is that they don’t get run each time the Workflow replays.

## How to add an Interceptor to a Plugin in Python

See [interceptors](https://docs.temporal.io/develop/python/interceptors) for the details of implementing interceptors.  To add one to a Plugin, do:

```python
class SomeWorkerInterceptor(
    temporalio.worker.Interceptor
):
    ...

class Plugin(temporalio.worker.Plugin):
		...
		
    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        config["interceptors"] = list(config.get("interceptors") or []) + [
            SomeWorkerInterceptor()
        ]
        return self.next_worker_plugin.configure_worker(config)
        
class Plugin(temporalio.client.Plugin):
		...
		
    def configure_client(self, config: ClientConfig) -> ClientConfig:
        config["interceptors"] = list(config.get("interceptors") or []) + [
            SomeClientInterceptor()
        ]
        return self.next_client_plugin.configure_client(config)
```

# AI Recipes

## I want to provide an agentic loop library.

**This section is under construction.**

An Agent is an example of a [Workflow Library](https://www.notion.so/Plugging-into-Workflows-Python-Integrator-s-Guide-2488fc56773880ebbafce42fe97b0c87?pvs=21).  It will be calling [tools](https://www.notion.so/Plugging-into-Workflows-Python-Integrator-s-Guide-2488fc56773880ebbafce42fe97b0c87?pvs=21) and [LLMs](https://www.notion.so/Plugging-into-Workflows-Python-Integrator-s-Guide-2488fc56773880ebbafce42fe97b0c87?pvs=21) and setting up [observability](https://www.notion.so/Plugging-into-Workflows-Python-Integrator-s-Guide-2488fc56773880ebbafce42fe97b0c87?pvs=21).

## I want to provide a built-in LLM.

LLMs can be called from within [built-in Activities](https://www.notion.so/Plugging-into-Workflows-Python-Integrator-s-Guide-2488fc56773880ebbafce42fe97b0c87?pvs=21) or [Nexus operations](https://www.notion.so/Plugging-into-Workflows-Python-Integrator-s-Guide-2488fc56773880ebbafce42fe97b0c87?pvs=21).  This example shows the former.

Here is the [input](https://github.com/temporalio/sdk-python/blob/11c2817d89da16e11aebd5704f0be4709568c189/temporalio/contrib/openai_agents/_invoke_model_activity.py#L135) to such an Activity used by the OpenAI Agents SDK.

And here is the [activity](https://github.com/temporalio/sdk-python/blob/11c2817d89da16e11aebd5704f0be4709568c189/temporalio/contrib/openai_agents/_invoke_model_activity.py#L163) for invoking their LLM.

Here’s what you need to know:

- All arguments and return values should be serializable.
- You’ll need to specify at least one timeout, most usefully [start_to_close_timeout](https://docs.temporal.io/encyclopedia/detecting-activity-failures#start-to-close-timeout), which will govern the timeout for each LLM call.  To calibrate your timeout, note that the shorter the timeout, the faster temporal will retry upon failure.
- Temporal should handle retries, not the LLM’s SDK.  To help it, make sure Temporal knows  which are retryable and non-retryable errors by annotating the errors or return values that are raised.

## I want to provide built-in Tools.

Most tools (i.e. those that hit network or are non-deterministic) should be called from within [built-in Activities](https://www.notion.so/Plugging-into-Workflows-Python-Integrator-s-Guide-2488fc56773880ebbafce42fe97b0c87?pvs=21) or [Nexus operations](https://www.notion.so/Plugging-into-Workflows-Python-Integrator-s-Guide-2488fc56773880ebbafce42fe97b0c87?pvs=21).  Trivial tools (e.g. calculators) can be done in the workflow.

To write activity tools, here’s what you need to know:

- All arguments and return values should be serializable.
- LLMs do not know how to set the retry policies and timeouts on their tool activities.
- Since at least one timeout is required, specify one when the tool is declared.  [start_to_close_timeout](https://docs.temporal.io/encyclopedia/detecting-activity-failures#start-to-close-timeout) is typically the  best practice.  To calibrate your timeout, note that the shorter the timeout, the faster temporal will retry upon failure.
- Temporal should handle retries, not the tool’s SDK.  To help it, make sure Temporal knows  which are retryable and non-retryable errors by annotating the errors or return values that are raised.

Here is an example of [OpenAI’s built-in tools being converted to activities](https://github.com/temporalio/sdk-python/blob/11c2817d89da16e11aebd5704f0be4709568c189/temporalio/contrib/openai_agents/workflow.py#L33).  This approach is used to make sure tool declarations are the same whether or not Temporal is being used.

## My users want to trace an Agentic Workflow and send data to my observability solution.

**This section is under construction.**