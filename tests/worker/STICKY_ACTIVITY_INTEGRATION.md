# Sticky activity SDK/server integration

These opt-in tests use the existing pytest external-server fixture, real Python
workers, the Rust bridge, and real workflow/activity RPCs. No separate harness is
required. They use the usual `new_worker` helper and default sandboxed workflow runner.

## Prerequisites and command

Run an isolated development server built from
[temporalio/temporal#12630](https://github.com/temporalio/temporal/pull/12630),
with `system.enableStickyActivityExecution` enabled for the test namespace.
The stock downloaded dev server and time-skipping server are not substitutes.
Server startup/database configuration belongs to that checkout; these tests do not
change dynamic configuration or start the custom server.

From the Python SDK checkout:

```bash
poe build-develop
TEMPORAL_TEST_STICKY_ACTIVITIES=1 poe test -s -n 0 \
  tests/worker/test_activity_sessions_integration.py \
  --workflow-environment 127.0.0.1:7233
```

Use `--workflow-environment envconfig` for an existing configured target/namespace.
Tests are skipped unless the environment variable is set and reject `local` and
`time-skipping` targets. They create uniquely named workflows and queues.

## Assertions

Each test starts a workflow/session worker and a competing activity-only worker on
the same normal queue using `new_worker`. Both register the same activity. A separate
connection gives the activity-only worker a distinct identity and independent
registration. Before starting the workflow, the test waits for that worker's
identity to appear in the normal activity queue's poller list.

The second client is an ordinary `Client.connect` connection with the same
namespace, authentication, transport, and converter settings; registering activities
without workflows makes its worker activity-only. Shared helpers in `tests.helpers`
handle poller readiness and history identity/attempt/completion assertions.

No isolation/stable-destination option is set: the server increment explicitly
rejects single-session isolation. Eager activity execution is disabled.

| Test | What success establishes |
| --- | --- |
| `test_session_activity_and_retry_stay_on_workflow_worker` | One workflow starts ten session activities concurrently. Each fails once; history records all ten starting on attempt two on the workflow worker and completing. Membership is persisted. |
| `test_session_activity_backlog_waits_for_worker_capacity` | Four session activities wait on a release event with only two available slots. The server retains pending backlog. After release, history records all four starting on the workflow worker on attempt one and completing. |

Slots are reserved before polling. An idle normal long poll can occupy a slot;
configured capacity is an upper bound, not a promise that every slot executes a
session activity concurrently. The backlog test does not assume exact utilization.
Synchronization uses activity-start events and an explicit release event, not sleeps.

## What this does not establish

This is validation of the current hot-worker routing subset, not the complete
Workflow-as-Session/AgentCore lifecycle. Worker loss, relocation, sticky reset,
sticky timeout fallback, lifetime isolation, Continue-As-New inheritance, server
rate-limit behavior, WCI lifecycle, and AgentCore invocation require separate
implementation/tests. Core unit tests cover SDK shutdown response ordering; these two
integration tests do not deliberately manufacture that server race.

The history identity assertions establish observed worker colocation despite a
competing normal-queue activity worker. They do not identify the physical queue of
each delivery or the worker for every intermediate retry attempt; intermediate
retries do not normally produce separate activity-start events in history. Combine
these checks with Core's poll-request encoding tests and the server PR's
sticky-queue dispatch tests.
