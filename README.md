# Temporal Python SDK

[![Python 3.7+](https://img.shields.io/pypi/pyversions/temporalio.svg?style=for-the-badge)](https://pypi.org/project/temporalio)
[![PyPI](https://img.shields.io/pypi/v/temporalio.svg?style=for-the-badge)](https://pypi.org/project/temporalio)
[![MIT](https://img.shields.io/pypi/l/temporalio.svg?style=for-the-badge)](LICENSE)

[Temporal](https://temporal.io/) is a distributed, scalable, durable, and highly available orchestration engine used to
execute asynchronous long-running business logic in a scalable and resilient way.

"Temporal Python SDK" is the framework for authoring workflows and activities using the Python programming language.

In addition to this documentation, see the [samples](https://github.com/temporalio/samples-python) repository for code
examples.

**⚠️ UNDER DEVELOPMENT**

The Python SDK is under development. There are no compatibility guarantees nor proper documentation pages at this time.

Currently missing features:

* Workflow worker support
* Async activity support (in client or worker)
* Support for Windows arm, macOS arm (i.e. M1), Linux arm, and Linux x64 glibc < 2.31.

## Quick Start

### Installation

Install the `temporalio` package from [PyPI](https://pypi.org/project/temporalio).

These steps can be followed to use with a virtual environment and `pip`:

* [Create a virtual environment](https://packaging.python.org/en/latest/tutorials/installing-packages/#creating-virtual-environments)
* Update `pip` - `python -m pip install -U pip`
  * Needed because older versions of `pip` may not pick the right wheel
* Install Temporal SDK - `python -m pip install temporalio`

The SDK is now ready for use.

### Starting a Workflow

Create the following script at `start_workflow.py`:

```python
import asyncio
from temporalio.client import Client

async def main():
  # Create client connected to server at the given address
  client = await Client.connect("http://localhost:7233")

  # Start a workflow
  handle = await client.start_workflow("my workflow name", "some arg", id="my-workflow-id", task_queue="my-task-queue")

  print(f"Workflow started with ID {handle.id}")

if __name__ == "__main__":
    asyncio.run(main())
```

Assuming you have a [Temporal server running on localhost](https://docs.temporal.io/docs/server/quick-install/), this
will start a workflow:

    python start_workflow.py

Note that an external worker has to be started with this workflow registered to actually run the workflow.

## Usage

### Client

A client can be created and used to start a workflow like so:

```python
from temporalio.client import Client

async def main():
  # Create client connected to server at the given address and namespace
  client = await Client.connect("http://localhost:7233", namespace="my-namespace")

  # Start a workflow
  handle = await client.start_workflow("my workflow name", "some arg", id="my-workflow-id", task_queue="my-task-queue")

  # Wait for result
  result = await handle.result()
  print(f"Result: {result}")
```

Some things to note about the above code:

* A `Client` does not have an explicit "close"
* Positional arguments can be passed to `start_workflow`
* The `handle` represents the workflow that was started and can be used for more than just getting the result
* Since we are just getting the handle and waiting on the result, we could have called `client.execute_workflow` which
  does the same thing
* Clients can have many more options not shown here (e.g. data converters and interceptors)

#### Data Conversion

Data converters are used to convert raw Temporal payloads to/from actual Python types. A custom data converter of type
`temporalio.converter.DataConverter` can be set via the `data_converter` client parameter.

The default data converter supports converting multiple types including:

* `None`
* `bytes`
* `google.protobuf.message.Message` - As JSON when encoding, but has ability to decode binary proto from other languages
* Anything that [`json.dump`](https://docs.python.org/3/library/json.html#json.dump) supports

As a special case in the default converter, [data classes](https://docs.python.org/3/library/dataclasses.html) are
automatically [converted to dictionaries](https://docs.python.org/3/library/dataclasses.html#dataclasses.asdict) before
encoding as JSON. Since Python is a dynamic language, when decoding via
[`json.load`](https://docs.python.org/3/library/json.html#json.load), the type is not known at runtime so, for example,
a JSON object will be a `dict`. As a special case, if the parameter type hint is a data class for a JSON payload, it is
decoded into an instance of that data class (properly recursing into child data classes).

### Activities

#### Activity-only Worker

An activity-only worker can be started like so:

```python
import asyncio
import logging
from temporalio.client import Client
from temporalio.worker import Worker

async def say_hello_activity(name: str) -> str:
    return f"Hello, {name}!"


async def main(stop_event: asyncio.Event):
  # Create client connected to server at the given address
  client = await Client.connect("http://localhost:7233", namespace="my-namespace")

  # Run the worker until the event is set
  worker = Worker(client, task_queue="my-task-queue", activities={"say-hello-activity": say_hello_activity})
  async with worker:
    await stop_event.wait()
```

Some things to note about the above code:

* This creates/uses the same client that is used for starting workflows
* The `say_hello_activity` is `async` which is the recommended activity type (see "Types of Activities" below)
* The created worker only runs activities, not workflows
* Activities are passed as a mapping with the key as a string activity name and the value as a callable
* While this example accepts a stop event and uses `async with`, `run()` and `shutdown()` may be used instead
* Workers can have many more options not shown here (e.g. data converters and interceptors)

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

###### Synchronous Multithreaded Activities

If `activity_executor` is set to an instance of `concurrent.futures.ThreadPoolExecutor` then the synchronous activities
are considered multithreaded activities. Besides `activity_executor`, no other worker parameters are required for
synchronous multithreaded activities.

###### Synchronous Multiprocess/Other Activities

Synchronous activities, i.e. functions that do not have `async def`, can be used with workers, but the
`activity_executor` worker parameter must be set with a `concurrent.futures.Executor` instance to use for executing the
activities. If this is _not_ set to an instance of `concurrent.futures.ThreadPoolExecutor` then the synchronous
activities are considered multiprocess/other activities.

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

## Development

The Python SDK is built to work with Python 3.7 and newer. It is built using
[SDK Core](https://github.com/temporalio/sdk-core/) which is written in Rust.

### Local development environment

- Install the system dependencies:

  - Python >=3.7
  - [pipx](https://github.com/pypa/pipx#install-pipx) (only needed for installing the two dependencies below)
  - [poetry](https://github.com/python-poetry/poetry) `pipx install poetry`
  - [poe](https://github.com/nat-n/poethepoet) `pipx install poethepoet`

- Use a local virtual env environment (helps IDEs and Windows):

  ```bash
  poetry config virtualenvs.in-project true
  ```

- Install the package dependencies (requires Rust):

  ```bash
  poetry install
  ```

- Build the project (requires Rust):

  ```bash
  poe build-develop
  ```

- Run the tests (requires Go):

  ```bash
  poe test
  ```