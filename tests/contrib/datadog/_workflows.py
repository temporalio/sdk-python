"""Workflow and activity definitions for Datadog tracing tests.

Kept separate from ``test_tracing.py`` so the workflow sandbox can re-import
workflow definitions without importing that test module's host-only ``ddtrace``
setup.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.contrib.datadog import (
    disconnect_trace_span_from_workflow_context,
    span_from_workflow_context,
)
from temporalio.exceptions import ApplicationError


@dataclass
class TestRequest:
    use_activity: bool = False
    use_local_activity: bool = False
    use_child_workflow: bool = False
    wait_for_kick: bool = False


@activity.defn
async def echo_activity(value: str) -> str:
    return value


@activity.defn
async def failing_activity() -> str:
    raise ApplicationError("boom")


@workflow.defn
class TestWorkflow:
    def __init__(self) -> None:
        self._kicked = False

    @workflow.run
    async def run(self, param: TestRequest) -> str:
        if param.use_activity:
            await workflow.execute_activity(
                echo_activity,
                "hello",
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        if param.use_local_activity:
            await workflow.execute_local_activity(
                echo_activity,
                "hello",
                start_to_close_timeout=timedelta(seconds=10),
            )
        if param.use_child_workflow:
            await workflow.execute_child_workflow(
                ChildWorkflow.run,
                "hello",
                id=f"{workflow.info().workflow_id}-child",
            )
        if param.wait_for_kick:
            await workflow.wait_condition(lambda: self._kicked)
        return "done"

    @workflow.signal
    def kick(self) -> None:
        self._kicked = True

    @workflow.query
    def get_status(self) -> str:
        return "running"


@workflow.defn
class UpdateTestWorkflow:
    def __init__(self) -> None:
        self._result: str | None = None

    @workflow.run
    async def run(self) -> str:
        await workflow.wait_condition(lambda: self._result is not None)
        return self._result or ""

    @workflow.update
    async def do_update(self, value: str) -> str:
        self._result = value
        return f"updated:{value}"

    @do_update.validator
    def validate_do_update(self, value: str) -> None:
        if value == "invalid":
            raise ApplicationError("invalid value")


@workflow.defn
class ChildWorkflow:
    @workflow.run
    async def run(self, value: str) -> str:
        return f"child:{value}"


@workflow.defn
class WaitingChildWorkflow:
    def __init__(self) -> None:
        self._done = False

    @workflow.run
    async def run(self) -> str:
        await workflow.wait_condition(lambda: self._done)
        return "done"

    @workflow.signal
    def finish(self) -> None:
        self._done = True


@workflow.defn
class ParentWithSignalChildWorkflow:
    """Starts a child, signals it via signal_child_workflow, awaits it, then waits for kick."""

    def __init__(self) -> None:
        self._kicked = False

    @workflow.run
    async def run(self) -> str:
        child_handle = await workflow.start_child_workflow(
            WaitingChildWorkflow.run,
            id=f"{workflow.info().workflow_id}-child",
        )
        await child_handle.signal(WaitingChildWorkflow.finish)
        await child_handle
        await workflow.wait_condition(lambda: self._kicked)
        return "done"

    @workflow.signal
    def kick(self) -> None:
        self._kicked = True

    @workflow.query
    def get_status(self) -> str:
        return "running"


@workflow.defn
class ParentWorkflow:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_child_workflow(
            ChildWorkflow.run,
            "hello",
            id=f"{workflow.info().workflow_id}-child",
        )


@workflow.defn
class LocalActivityWorkflow:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_local_activity(
            echo_activity,
            "hello",
            start_to_close_timeout=timedelta(seconds=10),
        )


@workflow.defn
class FailingWorkflow:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_activity(
            failing_activity,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


@workflow.defn
class ContinueAsNewWorkflow:
    @workflow.run
    async def run(self, iteration: int = 0) -> str:
        if iteration == 0:
            workflow.continue_as_new(1)
        return "done"


@workflow.defn
class DisconnectedContinueAsNewWorkflow:
    @workflow.run
    async def run(self, iteration: int = 0) -> str:
        if iteration == 0:
            disconnect_trace_span_from_workflow_context()
            workflow.continue_as_new(1)
        return "done"


@workflow.defn
class CustomTagWorkflow:
    def __init__(self) -> None:
        self._kicked = False

    @workflow.run
    async def run(self, wait_for_kick: bool = False) -> str:
        span = span_from_workflow_context()
        if span is not None:
            span.set_tag("custom.workflow.tag", "hello-from-workflow")
        if wait_for_kick:
            await workflow.wait_condition(lambda: self._kicked)
        return "done"

    @workflow.signal
    def kick(self) -> None:
        self._kicked = True

    @workflow.query
    def get_status(self) -> str:
        return "running"


@workflow.defn
class DirectlyFailingWorkflow:
    @workflow.run
    async def run(self) -> str:
        raise ApplicationError("workflow failed directly")


@activity.defn
async def logging_activity() -> str:
    activity.logger.info("test log message from activity")
    return "done"


@activity.defn
async def custom_span_activity() -> tuple[int | None, int | None]:
    """Create a custom ddtrace span and return its (parent_id, trace_id).

    Uses tracer.trace() which auto-parents from the active context span.
    """
    import ddtrace

    child = ddtrace.tracer.trace("custom.span")  # type: ignore[reportPrivateImportUsage]
    try:
        return (child.parent_id, child.trace_id)
    finally:
        child.finish()


@workflow.defn
class LoggingWorkflow:
    @workflow.run
    async def run(self) -> str:
        workflow.logger.info("test log message from workflow")
        return "done"


@workflow.defn
class CustomSpanActivityWorkflow:
    @workflow.run
    async def run(self) -> tuple[int | None, int | None]:
        return await workflow.execute_activity(
            custom_span_activity,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


@workflow.defn
class LoggingActivityWorkflow:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_activity(
            logging_activity,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


@workflow.defn
class ConcurrentLoggingWorkflow:
    """Logs before and after a signal barrier so two instances provably overlap.

    The test starts both workflows, waits until both have logged "start:<label>"
    (confirmed via is_started()), then signals both to proceed.  This guarantees
    they are in-flight at the same time before the second log is emitted.
    """

    def __init__(self) -> None:
        self._proceed = False
        self._started = False

    @workflow.run
    async def run(self, label: str) -> str:
        self._started = True
        workflow.logger.info(f"start:{label}")
        await workflow.wait_condition(lambda: self._proceed)
        workflow.logger.info(f"end:{label}")
        return "done"

    @workflow.signal
    def proceed(self) -> None:
        self._proceed = True

    @workflow.query
    def is_started(self) -> bool:
        return self._started
