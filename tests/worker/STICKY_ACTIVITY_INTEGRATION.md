# Sticky activity SDK/server integration

The opt-in tests in `test_activity_sessions.py` (marked `requires_session_server`) use the existing pytest external-server fixture, real Python
workers, the Rust bridge, and real workflow/activity RPCs. No separate harness is
required. They use the usual `new_worker` helper and default sandboxed workflow runner.

## Prerequisites and command

Run an isolated development server built from the `shivam/agentcore-single-session`
branch of [temporalio/temporal](https://github.com/temporalio/temporal), with
`system.enableStickyActivityExecution` enabled for the test namespace.
The stock downloaded dev server and time-skipping server are not substitutes.
Server startup/database configuration belongs to that checkout; these tests do not
change dynamic configuration or start the custom server.

From the Python SDK checkout:

```bash
poe build-develop
TEMPORAL_TEST_STICKY_ACTIVITIES=1 poe test -s -n 0 \
  tests/worker/test_activity_sessions.py \
  --workflow-environment 127.0.0.1:7233
```

Use `--workflow-environment envconfig` for an existing configured target/namespace.
They are skipped unless the environment variable is set and reject `local` and
`time-skipping` targets. They create uniquely named workflows and queues.

## Assertions

The affinity tests start a workflow/session worker and a competing activity-only worker on
the same normal queue using `new_worker`. Both register the same activity. A separate
connection gives the activity-only worker a distinct identity and independent
registration. Before starting the workflow, the test waits for that worker's
identity to appear in the normal activity queue's poller list.

The second client is an ordinary `Client.connect` connection with the same
namespace, authentication, transport, and converter settings; registering activities
without workflows makes its worker activity-only. Shared helpers in `tests.helpers`
handle poller readiness and history identity/attempt/completion assertions.

The affinity tests set no admission option (shared session workers). The replay
test also runs single-session workers with a stable identity. Eager activity
execution is disabled.

| Test | What success establishes |
| --- | --- |
| `test_session_activity_and_retry_stay_on_workflow_worker` | One workflow starts ten session activities concurrently. Each fails once; history records all ten starting on attempt two on the workflow worker and completing. Membership is persisted. |
| `test_session_activity_backlog_waits_for_worker_capacity` | Four session activities wait on a release event with only two available slots. The server retains pending backlog. After release, history records all four starting on the workflow worker on attempt one and completing. |
| `test_session_workflow_histories_replay` | Workflows with and without session activities run on shared and single-session workers, including one whose single-session worker is replaced by a new worker with the same stable identity between activities. History records `workflow_session` exactly when activities used a session scope. Each history replays with the session config it ran with; without session config, only histories without session activities replay. |

Slots are reserved before polling. An idle normal long poll can occupy a slot;
configured capacity is an upper bound, not a promise that every slot executes a
session activity concurrently. The backlog test does not assume exact utilization.
Synchronization uses activity-start events and an explicit release event, not sleeps.

## What this does not establish

This is validation of the current hot-worker routing subset, not the complete
Workflow-as-Session/AgentCore lifecycle. Worker crashes (the replay test
replaces a worker after a graceful shutdown), relocation, sticky reset, sticky
timeout fallback, Continue-As-New inheritance, server rate-limit behavior, WCI
lifecycle, and AgentCore invocation require separate implementation/tests. The
activity sessions sample in samples-python exercises single-session binding and a
crash (SIGKILL) and restart end to end. Core unit tests cover SDK shutdown response ordering; these two
integration tests do not deliberately manufacture that server race.

The history identity assertions establish observed worker colocation despite a
competing normal-queue activity worker. They do not identify the physical queue of
each delivery or the worker for every intermediate retry attempt; intermediate
retries do not normally produce separate activity-start events in history. Combine
these checks with Core's poll-request encoding tests and the server PR's
sticky-queue dispatch tests.
