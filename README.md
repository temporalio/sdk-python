# Temporal Python SDK

[![Python 3.7+](https://img.shields.io/pypi/pyversions/temporalio.svg?style=for-the-badge)](https://pypi.org/project/temporalio)
[![PyPI](https://img.shields.io/pypi/v/temporalio.svg?style=for-the-badge)](https://pypi.org/project/temporalio)
[![MIT](https://img.shields.io/pypi/l/temporalio.svg?style=for-the-badge)](LICENSE)

[Temporal](https://temporal.io/) is a distributed, scalable, durable, and highly available orchestration engine used to
execute asynchronous long-running business logic in a scalable and resilient way.

"Temporal Python SDK" is the framework for authoring workflows and activities using the Python programming language.

Also see:

* [Code Samples](https://github.com/temporalio/samples-python)
* [API Documentation](https://python.temporal.io)
* [Application Development Guide](https://docs.temporal.io/application-development?lang=python)

In addition to features common across all Temporal SDKs, the Python SDK also has the following interesting features:

**Type Safe**

This library uses the latest typing and MyPy support with generics to ensure all calls can be typed. For example,
starting a workflow with an `int` parameter when it accepts a `str` parameter would cause MyPy to fail.

**Different Activity Types**

The activity worker has been developed to work with `async def`, threaded, and multiprocess activities. While
`async def` activities are the easiest and recommended, care has been taken to make heartbeating and cancellation also
work across threads/processes.

**Custom `asyncio` Event Loop**

The workflow implementation basically turns `async def` functions into workflows backed by a distributed, fault-tolerant
event loop. This means task management, sleep, cancellation, etc have all been developed to seamlessly integrate with
`asyncio` concepts.

**⚠️ UNDER DEVELOPMENT**

The Python SDK is under development. There are no compatibility guarantees at this time.

## Quick Start

### Installation

Install the `temporalio` package from [PyPI](https://pypi.org/project/temporalio).

These steps can be followed to use with a virtual environment and `pip`:

* [Create a virtual environment](https://packaging.python.org/en/latest/tutorials/installing-packages/#creating-virtual-environments)
* Update `pip` - `python -m pip install -U pip`
  * Needed because older versions of `pip` may not pick the right wheel
* Install Temporal SDK - `python -m pip install temporalio`

The SDK is now ready for use. To build from source, see "Building" near the end of this documentation.

**NOTE: This README is for the current branch and not necessarily what's released on `PyPI`.**

### Implementing a Workflow

Create the following script at `run_worker.py`:

```python
import asyncio
from datetime import datetime, timedelta
from temporalio import workflow, activity
from temporalio.client import Client
from temporalio.worker import Worker

@activity.defn
async def say_hello(name: str) -> str:
    return f"Hello, {name}!"

@workflow.defn
class SayHello:
    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_activity(
            say_hello, name, schedule_to_close_timeout=timedelta(seconds=5)
        )

async def main():
    # Create client connected to server at the given address
    client = await Client.connect("localhost:7233")

    # Run the worker
    worker = Worker(client, task_queue="my-task-queue", workflows=[SayHello], activities=[say_hello])
    await worker.run()

if __name__ == "__main__":
    asyncio.run(main())
```

Assuming you have a [Temporal server running on localhost](https://docs.temporal.io/docs/server/quick-install/), this
will run the worker:

    python run_worker.py

### Running a Workflow

Create the following script at `run_workflow.py`:

```python
import asyncio
from temporalio.client import Client

# Import the workflow from the previous code
from run_worker import SayHello

async def main():
    # Create client connected to server at the given address
    client = await Client.connect("localhost:7233")

    # Execute a workflow
    result = await client.execute_workflow(SayHello.run, "my name", id="my-workflow-id", task_queue="my-task-queue")

    print(f"Result: {result}")

if __name__ == "__main__":
    asyncio.run(main())
```

Assuming you have `run_worker.py` running from before, this will run the workflow:

    python run_workflow.py

The output will be:

    Result: Hello, my-name!

## Usage

### Client

A client can be created and used to start a workflow like so:

```python
from temporalio.client import Client

async def main():
    # Create client connected to server at the given address and namespace
    client = await Client.connect("localhost:7233", namespace="my-namespace")

    # Start a workflow
    handle = await client.start_workflow(MyWorkflow.run, "some arg", id="my-workflow-id", task_queue="my-task-queue")

    # Wait for result
    result = await handle.result()
    print(f"Result: {result}")
```

Some things to note about the above code:

* A `Client` does not have an explicit "close"
* To enable TLS, the `tls` argument to `connect` can be set to `True` or a `TLSConfig` object
* A single positional argument can be passed to `start_workflow`. If there are multiple arguments, only the
  non-type-safe form of `start_workflow` can be used (i.e. the one accepting a string workflow name) and it must be in
  the `args` keyword argument.
* The `handle` represents the workflow that was started and can be used for more than just getting the result
* Since we are just getting the handle and waiting on the result, we could have called `client.execute_workflow` which
  does the same thing
* Clients can have many more options not shown here (e.g. data converters and interceptors)
* A string can be used instead of the method reference to call a workflow by name (e.g. if defined in another language)

Clients also provide a shallow copy of their config for use in making slightly different clients backed by the same
connection. For instance, given the `client` above, this is how to have a client in another namespace:

```python
config = client.config()
config["namespace"] = "my-other-namespace"
other_ns_client = Client(**config)
```

#### Data Conversion

Data converters are used to convert raw Temporal payloads to/from actual Python types. A custom data converter of type
`temporalio.converter.DataConverter` can be set via the `data_converter` client parameter. Data converters are a
combination of payload converters and payload codecs. The former converts Python values to/from serialized bytes, and
the latter converts bytes to bytes (e.g. for compression or encryption).

The default data converter supports converting multiple types including:

* `None`
* `bytes`
* `google.protobuf.message.Message` - As JSON when encoding, but has ability to decode binary proto from other languages
* Anything that can be converted to JSON including:
  * Anything that [`json.dump`](https://docs.python.org/3/library/json.html#json.dump) supports natively
  * [dataclasses](https://docs.python.org/3/library/dataclasses.html)
  * Iterables including ones JSON dump may not support by default, e.g. `set`
  * Any class with a `dict()` method and a static `parse_obj()` method, e.g.
    [Pydantic models](https://pydantic-docs.helpmanual.io/usage/models)
  * [IntEnum](https://docs.python.org/3/library/enum.html) based enumerates

For converting from JSON, the workflow/activity type hint is taken into account to convert to the proper type. Care has
been taken to support all common typings including `Optional`, `Union`, all forms of iterables and mappings, `NewType`,
etc in addition to the regular JSON values mentioned before.

Users are strongly encouraged to use a single `dataclass` for parameter and return types so fields with defaults can be
easily added without breaking compatibility.

### Workers

Workers host workflows and/or activities. Here's how to run a worker:

```python
import asyncio
import logging
from temporalio.client import Client
from temporalio.worker import Worker
# Import your own workflows and activities
from my_workflow_package import MyWorkflow, my_activity

async def run_worker(stop_event: asyncio.Event):
    # Create client connected to server at the given address
    client = await Client.connect("localhost:7233", namespace="my-namespace")

    # Run the worker until the event is set
    worker = Worker(client, task_queue="my-task-queue", workflows=[MyWorkflow], activities=[my_activity])
    async with worker:
        await stop_event.wait()
```

Some things to note about the above code:

* This creates/uses the same client that is used for starting workflows
* While this example accepts a stop event and uses `async with`, `run()` and `shutdown()` may be used instead
* Workers can have many more options not shown here (e.g. data converters and interceptors)

### Workflows

#### Definition

Workflows are defined as classes decorated with `@workflow.defn`. The method invoked for the workflow is decorated with
`@workflow.run`. Methods for signals and queries are decorated with `@workflow.signal` and `@workflow.query`
respectively. Here's an example of a workflow:

```python
import asyncio
from dataclasses import dataclass
from datetime import timedelta
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.worker import Worker

@dataclass
class GreetingInfo:
    salutation: str = "Hello"
    name: str = "<unknown>"

@workflow.defn
class GreetingWorkflow:
    def __init__() -> None:
        self._current_greeting = "<unset>"
        self._greeting_info = GreetingInfo()
        self._greeting_info_update = asyncio.Event()
        self._complete = asyncio.Event()

    @workflow.run
    async def run(self, name: str) -> str:
        self._greeting_info.name = name
        while True:
            # Store greeting
            self._current_greeting = await workflow.execute_activity(
                create_greeting_activity,
                self._greeting_info,
                start_to_close_timeout=timedelta(seconds=5),
            )
            workflow.logger.debug("Greeting set to %s", self._current_greeting)
            
            # Wait for salutation update or complete signal (this can be
            # cancelled)
            await asyncio.wait(
                [self._greeting_info_update.wait(), self._complete.wait()],
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self._complete.is_set():
                return self._current_greeting
            self._greeting_info_update.clear()

    @workflow.signal
    async def update_salutation(self, salutation: str) -> None:
        self._greeting_info.salutation = salutation
        self._greeting_info_update.set()

    @workflow.signal
    async def complete_with_greeting(self) -> None:
        self._complete.set()

    @workflow.query
    async def current_greeting(self) -> str:
        return self._current_greeting

@activity.defn
async def create_greeting_activity(info: GreetingInfo) -> str:
    return f"{info.salutation}, {info.name}!"
```

Some things to note about the above code:

* This workflow continually updates the queryable current greeting when signalled and can complete with the greeting on
  a different signal
* Workflows are always classes and must have a single `@workflow.run` which is an `async def` function
* Workflow code must be deterministic. This means no threading, no randomness, no external calls to processes, no
  network IO, and no global state mutation. All code must run in the implicit `asyncio` event loop and be deterministic.
* `@activity.defn` is explained in a later section. For normal simple string concatenation, this would just be done in
  the workflow. The activity is for demonstration purposes only.
* `workflow.execute_activity(create_greeting_activity, ...` is actually a typed signature, and MyPy will fail if the
  `self._greeting_info` parameter is not a `GreetingInfo`

Here are the decorators that can be applied:

* `@workflow.defn` - Defines a workflow class
  * Must be defined on the class given to the worker (ignored if present on a base class)
  * Can have a `name` param to customize the workflow name, otherwise it defaults to the unqualified class name
* `@workflow.run` - Defines the primary workflow run method
  * Must be defined on the same class as `@workflow.defn`, not a base class (but can _also_ be defined on the same
    method of a base class)
  * Exactly one method name must have this decorator, no more or less
  * Must be defined on an `async def` method
  * The method's arguments are the workflow's arguments
  * The first parameter must be `self`, followed by positional arguments. Best practice is to only take a single
    argument that is an object/dataclass of fields that can be added to as needed.
* `@workflow.signal` - Defines a method as a signal
  * Can be defined on an `async` or non-`async` function at any hierarchy depth, but if decorated method is overridden,
    the override must also be decorated
  * The method's arguments are the signal's arguments
  * Can have a `name` param to customize the signal name, otherwise it defaults to the unqualified method name
  * Can have `dynamic=True` which means all otherwise unhandled signals fall through to this. If present, cannot have
    `name` argument, and method parameters must be `self`, a string signal name, and a `*args` varargs param.
  * Non-dynamic method can only have positional arguments. Best practice is to only take a single argument that is an
    object/dataclass of fields that can be added to as needed.
  * Return value is ignored
* `@workflow.query` - Defines a method as a query
  * All the same constraints as `@workflow.signal` but should return a value
  * Temporal queries should never mutate anything in the workflow

#### Running

To start a locally-defined workflow from a client, you can simply reference its method like so:

```python
from temporalio.client import Client
from my_workflow_package import GreetingWorkflow

async def create_greeting(client: Client) -> str:
    # Start the workflow
    handle = await client.start_workflow(GreetingWorkflow.run, "my name", id="my-workflow-id", task_queue="my-task-queue")
    # Change the salutation
    await handle.signal(GreetingWorkflow.update_salutation, "Aloha")
    # Tell it to complete
    await handle.signal(GreetingWorkflow.complete_with_greeting)
    # Wait and return result
    return await handle.result()
```

Some things to note about the above code:

* This uses the `GreetingWorkflow` from the previous section
* The result of calling this function is `"Aloha, my name!"`
* `id` and `task_queue` are required for running a workflow
* `client.start_workflow` is typed, so MyPy would fail if `"my name"` were something besides a string
* `handle.signal` is typed, so MyPy would fail if `"Aloha"` were something besides a string or if we provided a
  parameter to the parameterless `complete_with_greeting`
* `handle.result` is typed to the workflow itself, so MyPy would fail if we said this `create_greeting` returned
  something besides a string

#### Invoking Activities

* Activities are started with non-async `workflow.start_activity()` which accepts either an activity function reference
  or a string name.
* A single argument to the activity is positional. Multiple arguments are not supported in the type-safe form of
  start/execute activity and must be supplied via the `args` keyword argument.
* Activity options are set as keyword arguments after the activity arguments. At least one of `start_to_close_timeout`
  or `schedule_to_close_timeout` must be provided.
* The result is an activity handle which is an `asyncio.Task` and supports basic task features
* An async `workflow.execute_activity()` helper is provided which takes the same arguments as
  `workflow.start_activity()` and `await`s on the result. This should be used in most cases unless advanced task
  capabilities are needed.
* Local activities work very similarly except the functions are `workflow.start_local_activity()` and
  `workflow.execute_local_activity()`
* Activities can be methods of a class. Invokers should use `workflow.start_activity_method()`,
  `workflow.execute_activity_method()`, `workflow.start_local_activity_method()`, and
  `workflow.execute_local_activity_method()` instead.
* Activities can callable classes (i.e. that define `__call__`). Invokers should use `workflow.start_activity_class()`,
  `workflow.execute_activity_class()`, `workflow.start_local_activity_class()`, and
  `workflow.execute_local_activity_class()` instead.

#### Invoking Child Workflows

* Child workflows are started with async `workflow.start_child_workflow()` which accepts either a workflow run method
  reference or a string name. The arguments to the workflow are positional.
* A single argument to the child workflow is positional. Multiple arguments are not supported in the type-safe form of
  start/execute child workflow and must be supplied via the `args` keyword argument.
* Child workflow options are set as keyword arguments after the arguments. At least `id` must be provided.
* The `await` of the start does not complete until the start has been accepted by the server
* The result is a child workflow handle which is an `asyncio.Task` and supports basic task features. The handle also has
  some child info and supports signalling the child workflow
* An async `workflow.execute_child_workflow()` helper is provided which takes the same arguments as
  `workflow.start_child_workflow()` and `await`s on the result. This should be used in most cases unless advanced task
  capabilities are needed.

#### Timers

* A timer is represented by normal `asyncio.sleep()`
* Timers are also implicitly started on any `asyncio` calls with timeouts (e.g. `asyncio.wait_for`)
* Timers are Temporal server timers, not local ones, so sub-second resolution rarely has value

#### Conditions

* `workflow.wait_condition` is an async function that doesn't return until a provided callback returns true
* A `timeout` can optionally be provided which will throw a `asyncio.TimeoutError` if reached (internally backed by
  `asyncio.wait_for` which uses a timer)

#### Asyncio and Cancellation

Workflows are backed by a custom [asyncio](https://docs.python.org/3/library/asyncio.html) event loop. This means many
of the common `asyncio` calls work as normal. Some asyncio features are disabled such as:

* Thread related calls such as `to_thread()`, `run_coroutine_threadsafe()`, `loop.run_in_executor()`, etc
* Calls that alter the event loop such as `loop.close()`, `loop.stop()`, `loop.run_forever()`,
  `loop.set_task_factory()`, etc
* Calls that use a specific time such as `loop.call_at()`
* Calls that use anything external such as networking, subprocesses, disk IO, etc

Cancellation is done the same way as `asyncio`. Specifically, a task can be requested to be cancelled but does not
necessarily have to respect that cancellation immediately. This also means that `asyncio.shield()` can be used to
protect against cancellation. The following tasks, when cancelled, perform a Temporal cancellation:

* Activities - when the task executing an activity is cancelled, a cancellation request is sent to the activity
* Child workflows - when the task starting or executing a child workflow is cancelled, a cancellation request is sent to
  cancel the child workflow
* Timers - when the task executing a timer is cancelled (whether started via sleep or timeout), the timer is cancelled

When the workflow itself is requested to cancel, `Task.cancel` is called on the main workflow task. Therefore,
`asyncio.CancelledError` can be caught in order to handle the cancel gracefully.

Workflows follow `asyncio` cancellation rules exactly which can cause confusion among Python developers. Cancelling a
task doesn't always cancel the thing it created. For example, given
`task = asyncio.create_task(workflow.start_child_workflow(...`, calling `task.cancel` does not cancel the child
workflow, it only cancels the starting of it, which has no effect if it has already started. However, cancelling the
result of `handle = await workflow.start_child_workflow(...` or
`task = asyncio.create_task(workflow.execute_child_workflow(...` _does_ cancel the child workflow.

Also, due to Temporal rules, a cancellation request is a state not an event. Therefore, repeated cancellation requests
are not delivered, only the first. If the workflow chooses swallow a cancellation, it cannot be requested again.

#### Workflow Utilities

While running in a workflow, in addition to features documented elsewhere, the following items are available from the
`temporalio.workflow` package:

* `continue_as_new()` - Async function to stop the workflow immediately and continue as new
* `info()` - Returns information about the current workflow
* `logger` - A logger for use in a workflow (properly skips logging on replay)
* `now()` - Returns the "current time" from the workflow's perspective

#### Exceptions

* Workflows can raise exceptions to fail the workflow
* Using `temporalio.exceptions.ApplicationError`, exceptions can be marked as non-retryable or include details

#### External Workflows

* `workflow.get_external_workflow_handle()` inside a workflow returns a handle to interact with another workflow
* `workflow.get_external_workflow_handle_for()` can be used instead for a type safe handle
* `await handle.signal()` can be called on the handle to signal the external workflow
* `await handle.cancel()` can be called on the handle to send a cancel to the external workflow

#### Testing

Workflow testing can be done in an integration-test fashion against a real server, however it is hard to simulate
timeouts and other long time-based code. Using the time-skipping workflow test environment can help there.

The time-skipping `temporalio.testing.WorkflowEnvironment` can be created via the static async `start_time_skipping()`.
This internally downloads the Temporal time-skipping test server to a temporary directory if it doesn't already exist,
then starts the test server which has special APIs for skipping time.

##### Automatic Time Skipping

Anytime a workflow result is waited on, the time-skipping server automatically advances to the next event it can. To
manually advance time before waiting on the result of a workflow, the `WorkflowEnvironment.sleep` method can be used.

Here's a simple example of a workflow that sleeps for 24 hours:

```python
import asyncio
from temporalio import workflow

@workflow.defn
class WaitADayWorkflow:
    @workflow.run
    async def run(self) -> str:
        await asyncio.sleep(24 * 60 * 60)
        return "all done"
```

An integration test of this workflow would be way too slow. However the time-skipping server automatically skips to the
next event when we wait on the result. Here's a test for that workflow:

```python
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

async def test_wait_a_day_workflow():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue="tq1", workflows=[WaitADayWorkflow]):
            assert "all done" == await env.client.execute_workflow(WaitADayWorkflow.run, id="wf1", task_queue="tq1")
```

That test will run almost instantly. This is because by calling `execute_workflow` on our client, we have asked the
environment to automatically skip time as much as it can (basically until the end of the workflow or until an activity
is run).

To disable automatic time-skipping while waiting for a workflow result, run code inside a
`with env.auto_time_skipping_disabled():` block.

##### Manual Time Skipping

Until a workflow is waited on, all time skipping in the time-skipping environment is done manually via
`WorkflowEnvironment.sleep`.

Here's workflow that waits for a signal or times out:

```python
import asyncio
from temporalio import workflow

@workflow.defn
class SignalWorkflow:
    def __init__(self) -> None:
        self.signal_received = False

    @workflow.run
    async def run(self) -> str:
        # Wait for signal or timeout in 45 seconds
        try:
            await workflow.wait_condition(lambda: self.signal_received, timeout=45)
            return "got signal"
        except asyncio.TimeoutError:
            return "got timeout"

    @workflow.signal
    def some_signal(self) -> None:
        self.signal_received = True
```

To test a normal signal, you might:

```python
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

async def test_signal_workflow():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue="tq1", workflows=[SignalWorkflow]):
            # Start workflow, send signal, check result
            handle = await env.client.start_workflow(SignalWorkflow.run, id="wf1", task_queue="tq1")
            await handle.signal(SignalWorkflow.some_signal)
            assert "got signal" == await handle.result()
```

But how would you test the timeout part? Like so:

```python
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

async def test_signal_workflow_timeout():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue="tq1", workflows=[SignalWorkflow]):
            # Start workflow, advance time past timeout, check result
            handle = await env.client.start_workflow(SignalWorkflow.run, id="wf1", task_queue="tq1")
            await env.sleep(50)
            assert "got timeout" == await handle.result()
```

Also, the current time of the workflow environment can be obtained via the async `WorkflowEnvironment.get_current_time`
method.

##### Mocking Activities

Activities are just functions decorated with `@activity.defn`. Simply write different ones and pass those to the worker
to have different activities called during the test.

### Activities

#### Definition

Activities are decorated with `@activity.defn` like so:

```python
from temporalio import activity

@activity.defn
async def say_hello_activity(name: str) -> str:
    return f"Hello, {name}!"
```

Some things to note about activity definitions:

* The `say_hello_activity` is `async` which is the recommended activity type (see "Types of Activities" below)
* A custom name for the activity can be set with a decorator argument, e.g. `@activity.defn(name="my activity")`
* Long running activities should regularly heartbeat and handle cancellation
* Activities can only have positional arguments. Best practice is to only take a single argument that is an
  object/dataclass of fields that can be added to as needed.
* Activities can be defined on methods instead of top-level functions. This allows the instance to carry state that an
  activity may need (e.g. a DB connection). The instance method should be what is registered with the worker.
* Activities can also be defined on callable classes (i.e. classes with `__call__`). An instance of the class should be
  what is registered with the worker.

#### Types of Activities

There are 3 types of activity callables accepted and described below: asynchronous, synchronous multithreaded, and
synchronous multiprocess/other. Only positional parameters are allowed in activity callables.

##### Asynchronous Activities

Asynchronous activities, i.e. functions using `async def`, are the recommended activity type. When using asynchronous
activities no special worker parameters are needed.

Cancellation for asynchronous activities is done via
[`asyncio.Task.cancel`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task.cancel). This means that
`asyncio.CancelledError` will be raised (and can be caught, but it is not recommended). An activity must heartbeat to
receive cancellation and there are other ways to be notified about cancellation (see "Activity Context" and
"Heartbeating and Cancellation" later).

##### Synchronous Activities

Synchronous activities, i.e. functions that do not have `async def`, can be used with workers, but the
`activity_executor` worker parameter must be set with a `concurrent.futures.Executor` instance to use for executing the
activities.

Cancellation for synchronous activities is done in the background and the activity must choose to listen for it and
react appropriately. An activity must heartbeat to receive cancellation and there are other ways to be notified about
cancellation (see "Activity Context" and "Heartbeating and Cancellation" later).

Note, all calls from an activity to functions in the `temporalio.activity` package are powered by
[contextvars](https://docs.python.org/3/library/contextvars.html). Therefore, new threads starting _inside_ of
activities must `copy_context()` and then `.run()` manually to ensure `temporalio.activity` calls like `heartbeat` still
function in the new threads.

###### Synchronous Multithreaded Activities

If `activity_executor` is set to an instance of `concurrent.futures.ThreadPoolExecutor` then the synchronous activities
are considered multithreaded activities. Besides `activity_executor`, no other worker parameters are required for
synchronous multithreaded activities.

###### Synchronous Multiprocess/Other Activities

If `activity_executor` is set to an instance of `concurrent.futures.Executor` that is _not_
`concurrent.futures.ThreadPoolExecutor`, then the synchronous activities are considered multiprocess/other activities.

These require special primitives for heartbeating and cancellation. The `shared_state_manager` worker parameter must be
set to an instance of `temporalio.worker.SharedStateManager`. The most common implementation can be created by passing a
`multiprocessing.managers.SyncManager` (i.e. result of `multiprocessing.managers.Manager()`) to
`temporalio.worker.SharedStateManager.create_from_multiprocessing()`.

Also, all of these activity functions must be
["picklable"](https://docs.python.org/3/library/pickle.html#what-can-be-pickled-and-unpickled).

#### Activity Context

During activity execution, an implicit activity context is set as a
[context variable](https://docs.python.org/3/library/contextvars.html). The context variable itself is not visible, but
calls in the `temporalio.activity` package make use of it. Specifically:

* `in_activity()` - Whether an activity context is present
* `info()` - Returns the immutable info of the currently running activity
* `heartbeat(*details)` - Record a heartbeat
* `is_cancelled()` - Whether a cancellation has been requested on this activity
* `wait_for_cancelled()` - `async` call to wait for cancellation request
* `wait_for_cancelled_sync(timeout)` - Synchronous blocking call to wait for cancellation request
* `is_worker_shutdown()` - Whether the worker has started graceful shutdown
* `wait_for_worker_shutdown()` - `async` call to wait for start of graceful worker shutdown
* `wait_for_worker_shutdown_sync(timeout)` - Synchronous blocking call to wait for start of graceful worker shutdown
* `raise_complete_async()` - Raise an error that this activity will be completed asynchronously (i.e. after return of
  the activity function in a separate client call)

With the exception of `in_activity()`, if any of the functions are called outside of an activity context, an error
occurs. Synchronous activities cannot call any of the `async` functions.

##### Heartbeating and Cancellation

In order for an activity to be notified of cancellation requests, they must invoke `temporalio.activity.heartbeat()`.
It is strongly recommended that all but the fastest executing activities call this function regularly. "Types of
Activities" has specifics on cancellation for asynchronous and synchronous activities.

In addition to obtaining cancellation information, heartbeats also support detail data that is persisted on the server
for retrieval during activity retry. If an activity calls `temporalio.activity.heartbeat(123, 456)` and then fails and
is retried, `temporalio.activity.info().heartbeat_details` will return an iterable containing `123` and `456` on the
next run.

##### Worker Shutdown

An activity can react to a worker shutdown. Using `is_worker_shutdown` or one of the `wait_for_worker_shutdown`
functions an activity can react to a shutdown.

When the `graceful_shutdown_timeout` worker parameter is given a `datetime.timedelta`, on shutdown the worker will
notify activities of the graceful shutdown. Once that timeout has passed (or if wasn't set), the worker will perform
cancellation of all outstanding activities.

The `shutdown()` invocation will wait on all activities to complete, so if a long-running activity does not at least
respect cancellation, the shutdown may never complete.

#### Testing

Unit testing an activity or any code that could run in an activity is done via the
`temporalio.testing.ActivityEnvironment` class. Simply instantiate this and any callable + params passed to `run` will
be invoked inside the activity context. The following are attributes/methods on the environment that can be used to
affect calls activity code might make to functions on the `temporalio.activity` package.

* `info` property can be set to customize what is returned from `activity.info()`
* `on_heartbeat` property can be set to handle `activity.heartbeat()` calls
* `cancel()` can be invoked to simulate a cancellation of the activity
* `worker_shutdown()` can be invoked to simulate a worker shutdown during execution of the activity

### Workflow Replay

Given a workflow's history, it can be replayed locally to check for things like non-determinism errors. For example,
assuming `history_json_str` is populated with a JSON string history either exported from the web UI or from `tctl`, the
following function will replay it:

```python
from temporalio.worker import Replayer

async def run_replayer(history_json_str: str):
  replayer = Replayer(workflows=[SayHello])
  await replayer.replay_workflow(history_json_str)
```

This will throw an error if any non-determinism is detected.

### OpenTelemetry Support

OpenTelemetry support requires the optional `opentelemetry` dependencies which are part of the `opentelemetry` extra.
When using `pip`, running

    pip install temporalio[opentelemetry]

will install needed dependencies. Then the `temporalio.contrib.opentelemetry.TracingInterceptor` can be created and set
as an interceptor on the `interceptors` argument of `Client.connect`. When set, spans will be created for all client
calls and for all activity and workflow invocations on the worker, spans will be created and properly serialized through
the server to give one proper trace for a workflow execution.

## Development

The Python SDK is built to work with Python 3.7 and newer. It is built using
[SDK Core](https://github.com/temporalio/sdk-core/) which is written in Rust.

### Building

#### Prepare

To build the SDK from source for use as a dependency, the following prerequisites are required:

* [Python](https://www.python.org/) >= 3.7
* [Rust](https://www.rust-lang.org/)
* [poetry](https://github.com/python-poetry/poetry) (e.g. `python -m pip install poetry`)
* [poe](https://github.com/nat-n/poethepoet) (e.g. `python -m pip install poethepoet`)

macOS note: If errors are encountered, it may be better to install Python and Rust as recommended from their websites
instead of via `brew`.

With the prerequisites installed, first clone the SDK repository recursively:

```bash
git clone --recursive https://github.com/temporalio/sdk-python.git
cd sdk-python
```

Use `poetry` to install the dependencies with `--no-root` to not install this package (because we still need to build
it):

```bash
poetry install --no-root
```

#### Build

Now perform the release build:

> This will take a while because Rust will compile the core project in release mode (see [Local SDK development
environment](#local-sdk-development-environment) for the quicker approach to local development).

```bash
poetry build
```

The compiled wheel doesn't have the exact right tags yet for use, so run this script to fix it:

```bash
poe fix-wheel
```

The `whl` wheel file in `dist/` is now ready to use.

#### Use

The wheel can now be installed into any virtual environment.

For example,
[create a virtual environment](https://packaging.python.org/en/latest/tutorials/installing-packages/#creating-virtual-environments)
somewhere and then run the following inside the virtual environment:

```bash
pip install wheel
```

```bash
pip install /path/to/cloned/sdk-python/dist/*.whl
```

Create this Python file at `example.py`:

```python
import asyncio
from temporalio import workflow, activity
from temporalio.client import Client
from temporalio.worker import Worker

@workflow.defn
class SayHello:
    @workflow.run
    async def run(self, name: str) -> str:
        return f"Hello, {name}!"

async def main():
    client = await Client.connect("localhost:7233")
    async with Worker(client, task_queue="my-task-queue", workflows=[SayHello]):
        result = await client.execute_workflow(SayHello.run, "Temporal",
            id="my-workflow-id", task_queue="my-task-queue")
        print(f"Result: {result}")

if __name__ == "__main__":
    asyncio.run(main())
```

Assuming there is a [local Temporal server](https://docs.temporal.io/docs/server/quick-install/) running, execute the
file with `python` (or `python3` if necessary):

```bash
python example.py
```

It should output:

    Result: Hello, Temporal!

### Local SDK development environment

For local development, it is often quicker to use debug builds and a local virtual environment.

While not required, it often helps IDEs if we put the virtual environment `.venv` directory in the project itself. This
can be configured system-wide via:

```bash
poetry config virtualenvs.in-project true
```

Now perform the same steps as the "Prepare" section above by installing the prerequisites, cloning the project,
installing dependencies, and generating the protobuf code:

```bash
git clone --recursive https://github.com/temporalio/sdk-python.git
cd sdk-python
poetry install --no-root
```

Now compile the Rust extension in develop mode which is quicker than release mode:

```bash
poe build-develop
```

That step can be repeated for any Rust changes made.

The environment is now ready to develop in.

#### Testing

Tests currently require [Go](https://go.dev/) to be installed since they use an embedded Temporal server as a library.
With `Go` installed, run the following to execute tests:

```bash
poe test
```

This runs against [Temporalite](https://github.com/temporalio/temporalite). To run against the time-skipping test
server, pass `--workflow-environment time-skipping`. To run against the `default` namespace of an already-running
server, pass the `host:port` to `--workflow-environment`.

### Style

* Mostly [Google Style Guide](https://google.github.io/styleguide/pyguide.html). Notable exceptions:
  * We use [Black](https://github.com/psf/black) for formatting, so that takes precedence
  * In tests and example code, can import individual classes/functions to make it more readable. Can also do this for
    rarely in library code for some Python common items (e.g. `dataclass` or `partial`), but not allowed to do this for
    any `temporalio` packages (except `temporalio.types`) or any classes/functions that aren't clear when unqualified.
  * We allow relative imports for private packages
  * We allow `@staticmethod`
