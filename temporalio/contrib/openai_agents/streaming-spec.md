# Streaming Support in OpenAI Agents SDK

<programmer instruction>
We want to add streaming support to the OpenAI Agents SDK. The problem is that Temporal doesn't have support for streaming yet, so we are going to need to make changes to the SDK to support it.

In the present prototype, we will make these changes entirely in the Temporal Python SDK. In the future we may also work on the core Ruby SDK. We will also be streaming from local activities, and not from Temporal workflows.

Phase I steps:
- extend the openai agents sdk tests to include tests of streaming functionality. Use the examples at /Users/jssmith/t/openai-agents-python/examples/basic to see how the API works. These tests will be failing initially (probably with "NotImplementedError" or a similar exception).
- add "batched conversion" implementation. Here you are not changing the SDK, but instead changing the temporalio/contrib/openai_agents/ plugin and taking the entire streaming response, turning it into list in the activity, and then feeding that back as an iterator in the workflow.
- verify that the tests are working.

Phase II steps:
- we will add a second implementation that will provide incremental streaming support by changing the SDK, rather than just batching the entire stream response as in Phase I.
- first focus on the Temporal Python SDK and don't worry about the OpenAI Agents SDK integration, i.e., make the core streaming primitives work.
- We introduce "streaming activities" which return AsyncIterable types. Such activities always run as local activities, for now.
- Activity results are made durable only when the activity has completed, and they are stored in the workflow event history as they always have been.
- Until the activity returns, its results are considered non-durable. To process these non-durable results, we introduce the concept of "peeking into the results of an activity".
- When invoking an async activity, you can set an option to make the results "peekable". When True, activity results come into the workflow even if they are not made durable.
- The workflow is now in "peek" mode, which has certain restrictions. It is performing speculative execution, i.e., the results are not made durable yet.
- For the time being, we allow the workflow to peek into one activity at a time only. starting a second activity in peek mode before reading the full result of the first will result result in an error. This ensures that we do not introduce non-determinism from interleaving of multiple iterables.
- Once the activity has returned, the workflow exits peek mode and resumes execution as normal. 

Future phases (deferred for now):
- Support for parallel streaming from multiple activities.
- Support streaming to the workflow.
- Support streaming from non-local activities.

</programmer instruction>