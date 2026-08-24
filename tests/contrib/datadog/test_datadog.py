"""Tests for the Datadog tracing interceptor."""

import asyncio
import logging
import uuid
from typing import Any

import pytest
from ddtrace.internal.utils.formats import format_trace_id
from ddtrace.trace import tracer as _dd_tracer

import temporalio.client
import temporalio.common
import temporalio.converter
from temporalio.client import Client
from temporalio.contrib.datadog import (
    DatadogTracingInterceptor,
    FinishContext,
    FinishResult,
)
from temporalio.contrib.datadog._id_generator import gen_span_id, gen_trace_id
from temporalio.contrib.datadog._workflow_interceptor import WorkflowTracingConfig
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.contrib.datadog._workflows import (
    ChildWorkflow,
    ConcurrentLoggingWorkflow,
    ContinueAsNewWorkflow,
    CustomSpanActivityWorkflow,
    CustomTagWorkflow,
    DirectlyFailingWorkflow,
    DisconnectedContinueAsNewWorkflow,
    FailingWorkflow,
    LocalActivityWorkflow,
    LoggingActivityWorkflow,
    LoggingWorkflow,
    ParentWithSignalChildWorkflow,
    ParentWorkflow,
    TestRequest,
    TestWorkflow,
    UpdateTestWorkflow,
    WaitingChildWorkflow,
    custom_span_activity,
    echo_activity,
    failing_activity,
    logging_activity,
)
from tests.contrib.datadog.conftest import (
    _make_interceptor,
    _SpanCollector,
    _task_queue,
    _traced_client,
)
from tests.helpers import LogCapturer


@pytest.mark.asyncio
async def test_workflow_spans(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """StartWorkflow and RunWorkflow spans are created with correct names and tags."""
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[TestWorkflow],
        activities=[echo_activity],
    ):
        wf_id = f"wf-{uuid.uuid4()}"
        handle = await tc.start_workflow(
            TestWorkflow.run,
            TestRequest(use_activity=False),
            id=wf_id,
            task_queue=tq,
        )
        await handle.result()

    start = span_collector.one("StartWorkflow")
    run = span_collector.one("RunWorkflow")

    assert start.resource == "TestWorkflow"
    assert run.resource == "TestWorkflow"

    # StartWorkflow is the trace root; RunWorkflow is its child.
    assert start.parent_id is None
    assert run.parent_id == start.span_id
    assert run.trace_id == start.trace_id

    # Tags on StartWorkflow
    assert span_collector.tag(start, "WorkflowID") == wf_id
    assert start.get_tag("span.kind") == "producer"

    # Tags on RunWorkflow
    assert span_collector.tag(run, "WorkflowID") == wf_id
    assert span_collector.tag(run, "WorkflowType") == "TestWorkflow"
    assert run.get_tag("span.kind") == "consumer"


@pytest.mark.asyncio
async def test_activity_spans(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """StartActivity and RunActivity spans are created with correct tags."""
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[TestWorkflow],
        activities=[echo_activity],
    ):
        handle = await tc.start_workflow(
            TestWorkflow.run,
            TestRequest(use_activity=True),
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        await handle.result()

    start_act = span_collector.one("StartActivity")
    run_act = span_collector.one("RunActivity")

    assert start_act.resource == "echo_activity"
    assert run_act.resource == "echo_activity"

    # Activity inbound span is a child of the outbound span.
    assert run_act.parent_id == start_act.span_id

    # RunActivity carries activity-specific tags.
    assert span_collector.tag(run_act, "ActivityType") == "echo_activity"
    assert span_collector.tag(run_act, "Attempt") is not None
    assert span_collector.tag(run_act, "ActivityID") is not None
    assert run_act.get_tag("span.kind") == "consumer"

    # The RunActivity span_id is deterministically derived from the idempotency key.
    run_id = span_collector.tag(run_act, "RunID")
    act_id = span_collector.tag(run_act, "ActivityID")
    attempt = span_collector.tag(run_act, "Attempt")
    expected_id = gen_span_id(f"{run_id}:{act_id}:{attempt}")
    assert run_act.span_id == expected_id


@pytest.mark.asyncio
async def test_uninstrumented_client_spans(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """Outbound spans are children of RunWorkflow when no DD header is present.

    Simulates a workflow started from an uninstrumented client (Temporal UI,
    scheduler, uninstrumented service) — no dd_trace_span in workflow headers.
    StartActivity must still be a child of RunWorkflow, not an independent root.
    """
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tq = _task_queue()

    # Use the plain client to start the workflow — no DD header injected.
    # Pass the interceptor only to the Worker so RunWorkflow/StartActivity
    # spans are created on the worker side without a propagated trace root.
    async with Worker(
        client,
        task_queue=tq,
        workflows=[TestWorkflow],
        activities=[echo_activity],
        interceptors=[interceptor],
    ):
        handle = await client.start_workflow(
            TestWorkflow.run,
            TestRequest(use_activity=True),
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        await handle.result()

    run = span_collector.one("RunWorkflow")
    start_act = span_collector.one("StartActivity")

    # StartActivity must be in the same trace as RunWorkflow.
    assert start_act.trace_id == run.trace_id, (
        "StartActivity started an independent trace instead of being a child of RunWorkflow"
    )
    # StartActivity must be a direct child of RunWorkflow.
    assert start_act.parent_id == run.span_id, (
        f"StartActivity.parent_id={start_act.parent_id} does not match RunWorkflow.span_id={run.span_id}"
    )
    # RunWorkflow is a trace root (no incoming DD header).
    assert run.parent_id is None


@pytest.mark.asyncio
async def test_uninstrumented_client_trace_id_stable_across_worker_restart(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """RunWorkflow trace_id is deterministic and stable across worker restarts
    when no DD header is present (uninstrumented client).

    Without a propagated trace_id, each worker restart would normally generate
    a fresh random trace_id, breaking APM correlation.  The interceptor instead
    derives a deterministic trace_id from the idempotency key so both workers
    produce the same value.
    """
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tq = _task_queue()
    wf_id = f"wf-{uuid.uuid4()}"

    # First worker: start the workflow (no DD header — plain client) and wait
    # until it is blocked on wait_condition so the RunWorkflow span is live.
    async with Worker(
        client,
        task_queue=tq,
        workflows=[TestWorkflow],
        activities=[echo_activity],
        interceptors=[interceptor],
    ):
        handle = await client.start_workflow(
            TestWorkflow.run,
            TestRequest(wait_for_kick=True),
            id=wf_id,
            task_queue=tq,
        )
        for _ in range(50):
            try:
                await handle.query(TestWorkflow.get_status)
                break
            except Exception:
                await asyncio.sleep(0.05)

    # Simulate process restart: discard the first worker's in-flight span.
    _dd_tracer._span_aggregator._traces.clear()

    # Second worker: replay history and complete the workflow.
    async with Worker(
        client,
        task_queue=tq,
        workflows=[TestWorkflow],
        activities=[echo_activity],
        interceptors=[interceptor],
    ):
        await handle.signal(TestWorkflow.kick)
        await handle.result()

    run = span_collector.one("RunWorkflow")

    run_id = handle.first_execution_run_id or ""
    key = f"WorkflowInboundInterceptor:default:{wf_id}:{run_id}:1"
    expected_trace_id = gen_trace_id(key)

    assert run.trace_id == expected_trace_id, (
        f"RunWorkflow trace_id {run.trace_id} does not match expected deterministic "
        f"value {expected_trace_id} for key {key!r} — trace_id is not stable across worker restarts"
    )
    assert run.parent_id is None, (
        "RunWorkflow must be a trace root when started without a DD header"
    )


@pytest.mark.asyncio
async def test_signal_spans(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """SignalWorkflow (client) and HandleSignal (worker) spans are created."""
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[TestWorkflow],
        activities=[echo_activity],
    ):
        handle = await tc.start_workflow(
            TestWorkflow.run,
            TestRequest(wait_for_kick=True),
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        await tc.get_workflow_handle(handle.id).signal(TestWorkflow.kick)
        await handle.result()

    sig_client = span_collector.one("SignalWorkflow")
    sig_worker = span_collector.one("HandleSignal")

    assert sig_client.resource == "kick"
    assert sig_worker.resource == "kick"
    assert sig_client.get_tag("span.kind") == "producer"
    assert sig_worker.get_tag("span.kind") == "consumer"
    assert sig_worker.parent_id == sig_client.span_id, (
        "HandleSignal must be a child of SignalWorkflow, not RunWorkflow"
    )


@pytest.mark.asyncio
async def test_handler_spans_fall_back_to_run_workflow_when_no_trace_header(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """Handler spans parent under RunWorkflow when the operation carries no trace header.

    Simulates an uninstrumented client sending a signal — no dd_trace_span in
    the signal headers.  HandleSignal must still be in the same trace and must
    be a child of RunWorkflow rather than an independent root.
    """
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[TestWorkflow],
        activities=[echo_activity],
    ):
        handle = await tc.start_workflow(
            TestWorkflow.run,
            TestRequest(wait_for_kick=True),
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        # Send the signal from the plain (uninstrumented) client — no DD headers injected.
        await client.get_workflow_handle(handle.id).signal(TestWorkflow.kick)
        await handle.result()

    run = span_collector.one("RunWorkflow")
    sig_worker = span_collector.one("HandleSignal")

    assert span_collector.by_op("SignalWorkflow") == [], (
        "Uninstrumented client must not produce a SignalWorkflow span"
    )
    assert sig_worker.trace_id == run.trace_id
    assert sig_worker.parent_id == run.span_id, (
        "HandleSignal must fall back to RunWorkflow as parent when signal carries no trace header"
    )


@pytest.mark.asyncio
async def test_handle_query_falls_back_to_run_workflow_when_no_trace_header(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """HandleQuery parents under RunWorkflow when the query carries no trace header."""
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[TestWorkflow],
        activities=[echo_activity],
    ):
        handle = await tc.start_workflow(
            TestWorkflow.run,
            TestRequest(wait_for_kick=True),
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        # Query from the plain (uninstrumented) client — no DD headers injected.
        await client.get_workflow_handle(handle.id).query(TestWorkflow.get_status)
        await client.get_workflow_handle(handle.id).signal(TestWorkflow.kick)
        await handle.result()

    run = span_collector.one("RunWorkflow")
    q_worker = span_collector.one("HandleQuery")

    assert span_collector.by_op("QueryWorkflow") == [], (
        "Uninstrumented client must not produce a QueryWorkflow span"
    )
    assert q_worker.trace_id == run.trace_id
    assert q_worker.parent_id == run.span_id, (
        "HandleQuery must fall back to RunWorkflow as parent when query carries no trace header"
    )


@pytest.mark.asyncio
async def test_handle_update_falls_back_to_run_workflow_when_no_trace_header(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """HandleUpdate and ValidateUpdate parent under RunWorkflow when the update carries no trace header."""
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[UpdateTestWorkflow],
    ):
        handle = await tc.start_workflow(
            UpdateTestWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        # Update from the plain (uninstrumented) client — no DD headers injected.
        await client.get_workflow_handle(handle.id).execute_update(
            UpdateTestWorkflow.do_update, "hello"
        )
        await handle.result()

    run = span_collector.one("RunWorkflow")
    validate = span_collector.one("ValidateUpdate")
    handle_upd = span_collector.one("HandleUpdate")

    assert span_collector.by_op("UpdateWorkflow") == [], (
        "Uninstrumented client must not produce an UpdateWorkflow span"
    )
    assert validate.trace_id == run.trace_id
    assert validate.parent_id == run.span_id, (
        "ValidateUpdate must fall back to RunWorkflow as parent when update carries no trace header"
    )
    assert handle_upd.trace_id == run.trace_id
    assert handle_upd.parent_id == run.span_id, (
        "HandleUpdate must fall back to RunWorkflow as parent when update carries no trace header"
    )


@pytest.mark.asyncio
async def test_query_spans(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """QueryWorkflow (client) and HandleQuery (worker) spans are created."""
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[TestWorkflow],
        activities=[echo_activity],
    ):
        handle = await tc.start_workflow(
            TestWorkflow.run,
            TestRequest(wait_for_kick=True),
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        result = await tc.get_workflow_handle(handle.id).query(TestWorkflow.get_status)
        assert result == "running"
        await tc.get_workflow_handle(handle.id).signal(TestWorkflow.kick)
        await handle.result()

    q_client = span_collector.one("QueryWorkflow")
    q_worker = span_collector.one("HandleQuery")

    assert q_client.resource == "get_status"
    assert q_worker.resource == "get_status"
    assert q_client.get_tag("span.kind") == "producer"
    assert q_worker.get_tag("span.kind") == "consumer"
    assert q_worker.parent_id == q_client.span_id, (
        "HandleQuery must be a child of QueryWorkflow, not RunWorkflow"
    )


@pytest.mark.asyncio
async def test_update_spans(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """UpdateWorkflow, ValidateUpdate, and HandleUpdate spans are created.

    The worker-side update spans carry the updateID tag.
    """
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    update_id = f"upd-{uuid.uuid4()}"

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[UpdateTestWorkflow],
    ):
        handle = await tc.start_workflow(
            UpdateTestWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        await handle.execute_update(
            UpdateTestWorkflow.do_update,
            "hello",
            id=update_id,
        )
        await handle.result()

    upd_client = span_collector.one("UpdateWorkflow")
    validate = span_collector.one("ValidateUpdate")
    handle_upd = span_collector.one("HandleUpdate")

    assert upd_client.resource == "do_update"
    assert validate.resource == "do_update"
    assert handle_upd.resource == "do_update"

    # Client span carries the update_id.
    assert span_collector.tag(upd_client, "UpdateID") == update_id

    # Worker spans also carry the update_id.
    assert span_collector.tag(validate, "UpdateID") == update_id
    assert span_collector.tag(handle_upd, "UpdateID") == update_id

    assert upd_client.get_tag("span.kind") == "producer"
    assert validate.get_tag("span.kind") == "consumer"
    assert handle_upd.get_tag("span.kind") == "consumer"

    assert validate.parent_id == upd_client.span_id, (
        "ValidateUpdate must be a child of UpdateWorkflow, not RunWorkflow"
    )
    assert handle_upd.parent_id == upd_client.span_id, (
        "HandleUpdate must be a child of UpdateWorkflow, not RunWorkflow"
    )

    # Verify that the update.id is the update request ID, not a child workflow ID.
    assert span_collector.tag(validate, "ChildWorkflowID") is None
    assert span_collector.tag(handle_upd, "ChildWorkflowID") is None


@pytest.mark.asyncio
async def test_run_workflow_span_id_is_deterministic(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """RunWorkflow span_id is derived from the FNV-64 idempotency key."""
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[TestWorkflow],
        activities=[echo_activity],
    ):
        handle = await tc.start_workflow(
            TestWorkflow.run,
            TestRequest(use_activity=False),
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        await handle.result()

    run = span_collector.one("RunWorkflow")

    # Counter starts at 1; RunWorkflow is always the first span.
    namespace = span_collector.tag(run, "Namespace") or "default"
    wf_id = span_collector.tag(run, "WorkflowID") or ""
    run_id = span_collector.tag(run, "RunID") or ""
    key = f"WorkflowInboundInterceptor:{namespace}:{wf_id}:{run_id}:1"
    assert run.span_id == gen_span_id(key)


@pytest.mark.asyncio
async def test_handle_signal_span_id_is_deterministic(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """HandleSignal span_id is derived from the FNV-64 idempotency key.

    The exact counter depends on task ordering (a signal that arrives in the
    same task as the workflow start may be processed before execute_workflow),
    so we verify that the span_id matches the hash for *some* valid counter
    rather than hard-coding counter=2.
    """
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[TestWorkflow],
        activities=[echo_activity],
    ):
        handle = await tc.start_workflow(
            TestWorkflow.run,
            TestRequest(wait_for_kick=True),
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        await tc.get_workflow_handle(handle.id).signal(TestWorkflow.kick)
        await handle.result()

    sig = span_collector.one("HandleSignal")

    namespace = span_collector.tag(sig, "Namespace") or "default"
    wf_id = span_collector.tag(sig, "WorkflowID") or ""
    run_id = span_collector.tag(sig, "RunID") or ""

    # Verify the span_id matches one of the first few counter values.
    valid_ids = {
        gen_span_id(f"WorkflowInboundInterceptor:{namespace}:{wf_id}:{run_id}:{n}")
        for n in range(1, 5)
    }
    assert sig.span_id in valid_ids, (
        f"HandleSignal span_id {sig.span_id} does not match any expected deterministic value (tried counters 1-4)"
    )


@pytest.mark.asyncio
async def test_failing_activity_marks_span_as_error(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """A failing activity records an error on the RunActivity span."""
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[FailingWorkflow],
        activities=[failing_activity],
    ):
        handle = await tc.start_workflow(
            FailingWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        with pytest.raises(Exception):
            await handle.result()

    run_act = span_collector.one("RunActivity")
    assert run_act.error == 1, "Expected RunActivity span to be marked as error"


@pytest.mark.asyncio
async def test_extra_tags(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """Custom extra_tags are applied to every span."""
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = DatadogTracingInterceptor(
        service_name="test-svc",
        extra_tags={"deployment.environment": "test", "team": "platform"},
    )
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[TestWorkflow],
        activities=[echo_activity],
    ):
        handle = await tc.start_workflow(
            TestWorkflow.run,
            TestRequest(use_activity=False),
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        await handle.result()

    run = span_collector.one("RunWorkflow")
    assert run.get_tag("deployment.environment") == "test"
    assert run.get_tag("team") == "platform"


@pytest.mark.asyncio
async def test_disable_signal_tracing(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """When disable_signal_tracing=True, no signal spans are created."""
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    config = WorkflowTracingConfig(
        disable_signal_tracing=True,
        disable_query_tracing=False,
        disable_update_tracing=False,
    )
    interceptor = _make_interceptor(workflow_tracing_config=config)
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[TestWorkflow],
        activities=[echo_activity],
    ):
        handle = await tc.start_workflow(
            TestWorkflow.run,
            TestRequest(wait_for_kick=True),
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        await tc.get_workflow_handle(handle.id).signal(TestWorkflow.kick)
        await handle.result()

    assert span_collector.by_op("SignalWorkflow") == [], (
        "Expected no SignalWorkflow span when signal tracing is disabled"
    )
    assert span_collector.by_op("HandleSignal") == [], (
        "Expected no HandleSignal span when signal tracing is disabled"
    )
    # RunWorkflow should still appear.
    span_collector.one("RunWorkflow")


@pytest.mark.asyncio
async def test_disable_query_tracing(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """When disable_query_tracing=True, no query spans are created."""
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    config = WorkflowTracingConfig(
        disable_signal_tracing=False,
        disable_query_tracing=True,
        disable_update_tracing=False,
    )
    interceptor = _make_interceptor(workflow_tracing_config=config)
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[TestWorkflow],
        activities=[echo_activity],
    ):
        handle = await tc.start_workflow(
            TestWorkflow.run,
            TestRequest(wait_for_kick=True),
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        await tc.get_workflow_handle(handle.id).query(TestWorkflow.get_status)
        await tc.get_workflow_handle(handle.id).signal(TestWorkflow.kick)
        await handle.result()

    assert span_collector.by_op("QueryWorkflow") == [], (
        "Expected no QueryWorkflow span when query tracing is disabled"
    )
    assert span_collector.by_op("HandleQuery") == [], (
        "Expected no HandleQuery span when query tracing is disabled"
    )


@pytest.mark.asyncio
async def test_disable_update_tracing(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """When disable_update_tracing=True, no update spans are created."""
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    config = WorkflowTracingConfig(
        disable_signal_tracing=False,
        disable_query_tracing=False,
        disable_update_tracing=True,
    )
    interceptor = _make_interceptor(workflow_tracing_config=config)
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[UpdateTestWorkflow],
    ):
        handle = await tc.start_workflow(
            UpdateTestWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        await handle.execute_update(UpdateTestWorkflow.do_update, "hi")
        await handle.result()

    assert span_collector.by_op("UpdateWorkflow") == [], (
        "Expected no UpdateWorkflow span when update tracing is disabled"
    )
    assert span_collector.by_op("ValidateUpdate") == [], (
        "Expected no ValidateUpdate span when update tracing is disabled"
    )
    assert span_collector.by_op("HandleUpdate") == [], (
        "Expected no HandleUpdate span when update tracing is disabled"
    )


@pytest.mark.asyncio
async def test_disable_update_tracing_still_propagates_into_new_workflow(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """With disable_update_tracing=True, update-with-start still injects the
    active trace into the new workflow's start headers.

    Without the fix the early return in start_update_with_start_workflow skips
    header injection entirely, so RunWorkflow has no parent even when there is
    an active caller trace.
    """
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    config = WorkflowTracingConfig(
        disable_signal_tracing=False,
        disable_query_tracing=False,
        disable_update_tracing=True,
    )
    interceptor = _make_interceptor(workflow_tracing_config=config)
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(tc, task_queue=tq, workflows=[UpdateTestWorkflow]):
        with _dd_tracer.trace("caller") as caller_span:
            start_op = temporalio.client.WithStartWorkflowOperation(
                UpdateTestWorkflow.run,
                id=f"wf-{uuid.uuid4()}",
                task_queue=tq,
                id_conflict_policy=temporalio.common.WorkflowIDConflictPolicy.FAIL,
            )
            await tc.execute_update_with_start_workflow(
                UpdateTestWorkflow.do_update,
                "hello",
                start_workflow_operation=start_op,
            )
            await (await start_op.workflow_handle()).result()

    run = span_collector.one("RunWorkflow")

    # No UpdateWithStart span should exist — update tracing is disabled.
    assert span_collector.by_op("UpdateWithStartWorkflow") == [], (
        "Expected no UpdateWithStartWorkflow span when update tracing is disabled"
    )
    # RunWorkflow must still be a child of the active caller span, not a root.
    assert run.trace_id == caller_span.trace_id, (
        "RunWorkflow started an independent trace even though a caller span was active"
    )
    assert run.parent_id == caller_span.span_id, (
        f"RunWorkflow.parent_id={run.parent_id} does not match caller span_id={caller_span.span_id}"
    )


@pytest.mark.asyncio
async def test_span_kind_tags(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """Every span carries the correct span.kind tag (producer / consumer)."""
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[TestWorkflow],
        activities=[echo_activity],
    ):
        handle = await tc.start_workflow(
            TestWorkflow.run,
            TestRequest(use_activity=True),
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        await handle.result()

    expected = {
        "StartWorkflow": "producer",
        "RunWorkflow": "consumer",
        "StartActivity": "producer",
        "RunActivity": "consumer",
    }
    for op, kind in expected.items():
        spans = span_collector.by_op(op)
        assert spans, f"Expected at least one '{op}' span"
        assert spans[0].get_tag("span.kind") == kind, (
            f"Expected span.kind={kind!r} for {op}"
        )


@pytest.mark.asyncio
async def test_baggage_service_name_same_service(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """set_baggage propagates the service name through headers.

    When client and worker share the same service name the consumer spans must
    NOT carry a peer.service tag (the annotator only sets it when the parent
    service differs from the current one).
    """
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()  # service_name="test-svc" for both sides
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[TestWorkflow],
        activities=[echo_activity],
    ):
        handle = await tc.start_workflow(
            TestWorkflow.run,
            TestRequest(use_activity=True),
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        await handle.result()

    for span in span_collector.spans:
        assert span.context.get_baggage_item("servicename") == "test-svc", (
            f"Expected servicename baggage on span {span.name}"
        )

    run_wf = span_collector.one("RunWorkflow")
    run_act = span_collector.one("RunActivity")
    assert run_wf.get_tag("peer.service") is None
    assert run_act.get_tag("peer.service") is None


@pytest.mark.asyncio
async def test_baggage_peer_service_cross_service(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """peer.service is set on consumer spans when client and worker differ.

    The client interceptor injects its service name into baggage via
    set_baggage. The worker interceptor reads it via get_baggage and the
    annotator sets peer.service when it differs from the worker service name.
    """
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    client_interceptor = DatadogTracingInterceptor(service_name="client-svc")
    worker_interceptor = DatadogTracingInterceptor(service_name="worker-svc")
    tc_client = _traced_client(client, client_interceptor)
    tc_worker = _traced_client(client, worker_interceptor)
    tq = _task_queue()

    async with Worker(
        tc_worker,
        task_queue=tq,
        workflows=[TestWorkflow],
        activities=[echo_activity],
    ):
        handle = await tc_client.start_workflow(
            TestWorkflow.run,
            TestRequest(use_activity=True),
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        await handle.result()

    start_wf = span_collector.one("StartWorkflow")
    run_wf = span_collector.one("RunWorkflow")
    run_act = span_collector.one("RunActivity")

    assert start_wf.context.get_baggage_item("servicename") == "client-svc"
    # RunWorkflow's parent is StartWorkflow (client-svc) → peer.service is set.
    assert run_wf.get_tag("peer.service") == "client-svc"
    # RunActivity's parent is StartActivity (also worker-svc) → no peer.service.
    assert run_act.get_tag("peer.service") is None


@pytest.mark.asyncio
async def test_trace_context_propagation(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """Trace context is propagated through Temporal headers across all boundaries.

    All spans for a single workflow execution must share one trace_id, and the
    parent-child chain must be unbroken:
      StartWorkflow → RunWorkflow → StartActivity → RunActivity
    """
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[TestWorkflow],
        activities=[echo_activity],
    ):
        handle = await tc.start_workflow(
            TestWorkflow.run,
            TestRequest(use_activity=True),
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        await handle.result()

    start_wf = span_collector.one("StartWorkflow")
    run_wf = span_collector.one("RunWorkflow")
    start_act = span_collector.one("StartActivity")
    run_act = span_collector.one("RunActivity")

    trace_id = start_wf.trace_id
    assert trace_id is not None

    # Every span belongs to the same trace.
    assert run_wf.trace_id == trace_id
    assert start_act.trace_id == trace_id
    assert run_act.trace_id == trace_id

    # Parent-child chain is intact across the header boundary.
    assert run_wf.parent_id == start_wf.span_id  # client → worker (workflow headers)
    assert (
        start_act.parent_id == run_wf.span_id
    )  # workflow outbound → activity scheduled
    assert run_act.parent_id == start_act.span_id  # activity headers → activity worker


@pytest.mark.asyncio
async def test_child_workflow_spans(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """Child workflow spans form a continuous chain from the parent trace.

    StartChildWorkflow is a child of the parent RunWorkflow. The child's own
    RunWorkflow is a child of StartChildWorkflow, keeping everything in one trace:
      StartWorkflow → RunWorkflow(parent) → StartChildWorkflow → RunWorkflow(child)
    """
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[ParentWorkflow, ChildWorkflow],
    ):
        handle = await tc.start_workflow(
            ParentWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        await handle.result()

    start_wf = span_collector.one("StartWorkflow")
    start_child = span_collector.one("StartChildWorkflow")
    run_wfs = span_collector.by_op("RunWorkflow")
    assert len(run_wfs) == 2, f"Expected 2 RunWorkflow spans, got {len(run_wfs)}"
    run_parent = next(s for s in run_wfs if s.resource == "ParentWorkflow")
    run_child = next(s for s in run_wfs if s.resource == "ChildWorkflow")

    trace_id = start_wf.trace_id
    assert start_child.trace_id == trace_id
    assert run_parent.trace_id == trace_id
    assert run_child.trace_id == trace_id

    assert (
        run_parent.parent_id == start_wf.span_id
    )  # workflow headers → RunWorkflow(parent)
    assert (
        start_child.parent_id == run_parent.span_id
    )  # parent RunWorkflow → StartChildWorkflow
    assert (
        run_child.parent_id == start_child.span_id
    )  # child workflow headers → RunWorkflow(child)

    assert start_child.resource == "ChildWorkflow"
    assert start_child.get_tag("span.kind") == "producer"
    assert run_child.get_tag("span.kind") == "consumer"


@pytest.mark.asyncio
async def test_failing_workflow_marks_span_as_error(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """A workflow that raises records an error on the RunWorkflow span."""
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[DirectlyFailingWorkflow],
    ):
        handle = await tc.start_workflow(
            DirectlyFailingWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        with pytest.raises(Exception):
            await handle.result()

    run_wf = span_collector.one("RunWorkflow")
    assert run_wf.error == 1, "Expected RunWorkflow span to be marked as error"
    assert run_wf.get_tag("error.type") is not None


@pytest.mark.asyncio
async def test_continue_as_new(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """ContinueAsNew sets the continued_as_new tag and does not mark the span as error."""
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[ContinueAsNewWorkflow],
    ):
        handle = await tc.start_workflow(
            ContinueAsNewWorkflow.run,
            0,
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        await handle.result()

    run_wfs = span_collector.by_op("RunWorkflow")
    assert len(run_wfs) == 2, (
        f"Expected 2 RunWorkflow spans (one per run), got {len(run_wfs)}"
    )

    continued = next(s for s in run_wfs if s.get_tag("temporal.continued_as_new"))
    completed = next(s for s in run_wfs if not s.get_tag("temporal.continued_as_new"))

    assert continued.get_tag("temporal.continued_as_new") is not None
    assert continued.error == 0, "ContinueAsNew should not mark the span as error"
    assert completed.error == 0

    # The second execution is a child of the first RunWorkflow span (same as Go SDK).
    assert completed.parent_id == continued.span_id
    assert completed.trace_id == continued.trace_id


@pytest.mark.asyncio
async def test_on_span_finish_callback(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """on_span_finish is called for every span and its extra_tags are applied."""
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    finished: list[FinishContext] = []

    def on_finish(ctx: FinishContext) -> FinishResult | None:
        finished.append(ctx)
        return FinishResult(extra_tags={"custom.operation": ctx.operation})

    interceptor = DatadogTracingInterceptor(
        service_name="test-svc",
        on_span_finish=on_finish,
    )
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[TestWorkflow],
        activities=[echo_activity],
    ):
        handle = await tc.start_workflow(
            TestWorkflow.run,
            TestRequest(use_activity=True),
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        await handle.result()

    # Callback was invoked for every span.
    finished_ops = {ctx.operation for ctx in finished}
    assert "StartWorkflow" in finished_ops
    assert "RunWorkflow" in finished_ops
    assert "StartActivity" in finished_ops
    assert "RunActivity" in finished_ops

    # No callback received a non-None exception for a successful run.
    assert all(ctx.exception is None for ctx in finished)

    # extra_tags from FinishResult are applied to each span.
    for span in span_collector.spans:
        op = span.name.removeprefix("temporal.")
        assert span.get_tag("custom.operation") == op, (
            f"Expected custom.operation={op!r} on span {span.name}"
        )


@pytest.mark.asyncio
async def test_on_span_finish_callback_receives_exception(
    client: Client,
    env: WorkflowEnvironment,
) -> None:
    """on_span_finish receives the exception on the FinishContext when a span fails."""
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    finish_contexts: list[FinishContext] = []

    def on_finish(ctx: FinishContext) -> FinishResult | None:
        finish_contexts.append(ctx)
        return None

    interceptor = DatadogTracingInterceptor(
        service_name="test-svc",
        on_span_finish=on_finish,
    )
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[DirectlyFailingWorkflow],
    ):
        handle = await tc.start_workflow(
            DirectlyFailingWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        with pytest.raises(Exception):
            await handle.result()

    run_wf_ctx = next(ctx for ctx in finish_contexts if ctx.operation == "RunWorkflow")
    assert run_wf_ctx.exception is not None
    assert "workflow failed directly" in str(run_wf_ctx.exception)

    # The client-side StartWorkflow span finishes without an exception.
    start_wf_ctx = next(
        ctx for ctx in finish_contexts if ctx.operation == "StartWorkflow"
    )
    assert start_wf_ctx.exception is None


@pytest.mark.asyncio
async def test_worker_restart_recovery(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """RunWorkflow span is recovered correctly after a worker restart."""
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()
    wf_id = f"wf-{uuid.uuid4()}"

    # First worker: start the workflow and wait until it is blocked on
    # wait_condition (i.e., the initial workflow task has completed).  We poll
    # with a query because that only succeeds once execute_workflow is live.
    async with Worker(
        tc,
        task_queue=tq,
        workflows=[TestWorkflow],
        activities=[echo_activity],
    ):
        handle = await tc.start_workflow(
            TestWorkflow.run,
            TestRequest(wait_for_kick=True),
            id=wf_id,
            task_queue=tq,
        )
        for _ in range(50):
            try:
                await handle.query(TestWorkflow.get_status)
                break
            except Exception:
                await asyncio.sleep(0.05)

        # Capture the server-side start time while the first worker is still alive.
        description = await handle.describe()
        expected_start_ns = int(description.start_time.timestamp() * 1e9)

    # Simulate process restart: discard all in-flight spans from the dead worker.
    # In production the ddtrace singleton is destroyed with the process, so these
    # spans are never sent. Clearing _traces here reproduces that behaviour and also
    # unblocks the trace flush for the recovery spans (they share the same trace_id
    # as the abandoned span, so without this the aggregator would wait for the
    # abandoned span to finish before flushing the recovery span).
    _dd_tracer._span_aggregator._traces.clear()

    # Second worker: replay the workflow history, receive the signal, complete.
    async with Worker(
        tc,
        task_queue=tq,
        workflows=[TestWorkflow],
        activities=[echo_activity],
    ):
        await handle.signal(TestWorkflow.kick)
        await handle.result()

    # Exactly one RunWorkflow span — recovered on the second worker.
    run = span_collector.one("RunWorkflow")

    # Verify the deterministic span_id using the known workflow_id, run_id from
    # the handle, and "default" namespace (test server default).
    run_id = handle.first_execution_run_id or ""
    key = f"WorkflowInboundInterceptor:default:{wf_id}:{run_id}:1"
    assert run.span_id == gen_span_id(key), (
        f"Recovered RunWorkflow span_id {run.span_id} does not match expected deterministic value for key {key!r}"
    )

    # The recovered span must carry workflow tags (set by the annotation fix).
    assert span_collector.tag(run, "WorkflowID") == wf_id
    assert span_collector.tag(run, "RunID") == run_id

    # The recovered span's start_ns must exactly match the server-recorded workflow
    # start time (workflow initialization time, not first-task execution time).
    assert run.start_ns == expected_start_ns, (
        f"RunWorkflow start_ns {run.start_ns} does not match original workflow "
        f"start time {expected_start_ns} — start time was not preserved across worker restart"
    )

    # The recovered span must still be a child of the original StartWorkflow.
    start = span_collector.one("StartWorkflow")
    assert run.parent_id == start.span_id


async def _wait_for_history_events(handle: Any, *event_fields: str) -> None:
    """Poll workflow history until all named event fields appear at least once."""
    for _ in range(100):
        history = await handle.fetch_history()
        if all(
            any(e.HasField(field) for e in history.events) for field in event_fields
        ):
            return
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_no_duplicate_outbound_spans_on_worker_restart(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """StartActivity, StartLocalActivity, and StartChildWorkflow spans are not re-emitted on replay."""
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()
    wf_id = f"wf-{uuid.uuid4()}"

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[TestWorkflow, ChildWorkflow],
        activities=[echo_activity],
    ):
        handle = await tc.start_workflow(
            TestWorkflow.run,
            TestRequest(
                use_activity=True,
                use_local_activity=True,
                use_child_workflow=True,
                wait_for_kick=True,
            ),
            id=wf_id,
            task_queue=tq,
        )
        # Wait until all three outbound operations are recorded in history.
        await _wait_for_history_events(
            handle,
            "activity_task_completed_event_attributes",
            "marker_recorded_event_attributes",
            "child_workflow_execution_completed_event_attributes",
        )

    _dd_tracer._span_aggregator._traces.clear()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[TestWorkflow, ChildWorkflow],
        activities=[echo_activity],
    ):
        await handle.signal(TestWorkflow.kick)
        await handle.result()

    assert span_collector.by_op("StartActivity") == [], (
        "Worker restart emitted duplicate StartActivity/StartLocalActivity span(s) during replay"
    )
    assert span_collector.by_op("StartChildWorkflow") == [], (
        "Worker restart emitted duplicate StartChildWorkflow span(s) during replay"
    )


@pytest.mark.asyncio
async def test_no_duplicate_signal_child_workflow_spans_on_worker_restart(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """SignalChildWorkflow spans are not re-emitted when a worker replays history."""
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()
    wf_id = f"wf-{uuid.uuid4()}"

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[ParentWithSignalChildWorkflow, WaitingChildWorkflow],
        activities=[],
    ):
        handle = await tc.start_workflow(
            ParentWithSignalChildWorkflow.run,
            id=wf_id,
            task_queue=tq,
        )
        # Wait until the child has completed (signal received and child done).
        await _wait_for_history_events(
            handle,
            "child_workflow_execution_completed_event_attributes",
        )

    _dd_tracer._span_aggregator._traces.clear()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[ParentWithSignalChildWorkflow, WaitingChildWorkflow],
        activities=[],
    ):
        await handle.signal(ParentWithSignalChildWorkflow.kick)
        await handle.result()

    assert span_collector.by_op("SignalChildWorkflow") == [], (
        "Worker restart emitted duplicate SignalChildWorkflow span(s) during replay"
    )
    assert span_collector.by_op("StartChildWorkflow") == [], (
        "Worker restart emitted duplicate StartChildWorkflow span(s) during replay"
    )


@pytest.mark.asyncio
async def test_local_activity_spans(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """Local activities produce a StartActivity span with temporal.local=True."""
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[LocalActivityWorkflow],
        activities=[echo_activity],
    ):
        handle = await tc.start_workflow(
            LocalActivityWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        await handle.result()

    start_act = span_collector.one("StartActivity")
    run_act = span_collector.one("RunActivity")

    assert start_act.resource == "echo_activity"
    assert run_act.resource == "echo_activity"
    assert span_collector.tag(start_act, "Local") == "True"
    assert run_act.parent_id == start_act.span_id


@pytest.mark.asyncio
async def test_allow_invalid_parent_spans(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """With allow_invalid_parent_spans=True, a malformed trace header is ignored
    and the workflow still produces a RunWorkflow span (with no parent)."""
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    payload_converter = temporalio.converter.PayloadConverter.default
    bad_header = payload_converter.to_payloads(
        [{"x-datadog-trace-id": "not-a-number"}]
    )[0]

    class _BadHeaderOutbound(temporalio.client.OutboundInterceptor):
        async def start_workflow(
            self, input: temporalio.client.StartWorkflowInput
        ) -> Any:
            input.headers = {**input.headers, "dd_trace_span": bad_header}
            return await super().start_workflow(input)

    class _BadHeaderInterceptor(temporalio.client.Interceptor):
        def intercept_client(
            self, next: temporalio.client.OutboundInterceptor
        ) -> temporalio.client.OutboundInterceptor:
            return _BadHeaderOutbound(next)

    interceptor = DatadogTracingInterceptor(
        service_name="test-svc",
        allow_invalid_parent_spans=True,
    )
    cfg = client.config()
    cfg["interceptors"] = [interceptor, _BadHeaderInterceptor()]
    tc = Client(**cfg)
    tq = _task_queue()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[TestWorkflow],
        activities=[echo_activity],
    ):
        handle = await tc.start_workflow(
            TestWorkflow.run,
            TestRequest(use_activity=False),
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        await handle.result()

    run = span_collector.one("RunWorkflow")
    assert run.parent_id is None or run.parent_id == 0


@pytest.mark.asyncio
async def test_disconnect_trace_span_from_workflow_context(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """disconnect_trace_span_from_workflow_context() causes the next execution to start
    a fresh root span rather than inheriting the current trace."""
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[DisconnectedContinueAsNewWorkflow],
    ):
        handle = await tc.start_workflow(
            DisconnectedContinueAsNewWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        await handle.result()

    runs = span_collector.by_op("RunWorkflow")
    assert len(runs) == 2

    first_run, second_run = sorted(runs, key=lambda s: s.start_ns)

    # First execution is a child of StartWorkflow.
    start = span_collector.one("StartWorkflow")
    assert first_run.parent_id == start.span_id

    # Second execution has no parent — the trace was disconnected.
    assert second_run.parent_id is None or second_run.parent_id == 0
    assert second_run.trace_id != first_run.trace_id


@pytest.mark.asyncio
async def test_span_from_workflow_context(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """span_from_workflow_context() lets workflow code tag the active RunWorkflow span."""
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(tc, task_queue=tq, workflows=[CustomTagWorkflow]):
        handle = await tc.start_workflow(
            CustomTagWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        await handle.result()

    run = span_collector.one("RunWorkflow")
    assert run.get_tag("custom.workflow.tag") == "hello-from-workflow"


@pytest.mark.asyncio
async def test_span_from_workflow_context_tag_preserved_after_worker_restart(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """Custom tags set via span_from_workflow_context() are preserved across a
    worker restart.  RunWorkflow always creates a live span (no is_replaying()
    guard), so user code re-runs set_tag during replay on the second worker,
    matching the behaviour of the Go SDK.
    """
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()
    wf_id = f"wf-{uuid.uuid4()}"

    # First worker: run until the workflow is blocked on wait_condition so the
    # custom tag has been set on the live span but the span is not yet finished.
    async with Worker(tc, task_queue=tq, workflows=[CustomTagWorkflow]):
        handle = await tc.start_workflow(
            CustomTagWorkflow.run,
            True,
            id=wf_id,
            task_queue=tq,
        )
        for _ in range(50):
            try:
                await handle.query(CustomTagWorkflow.get_status)
                break
            except Exception:
                await asyncio.sleep(0.05)

    # Simulate process restart: discard all in-flight spans from the dead worker.
    _dd_tracer._span_aggregator._traces.clear()

    # Second worker: replay creates a live span, user code re-runs set_tag,
    # then the kick signal is received and the workflow completes.
    async with Worker(tc, task_queue=tq, workflows=[CustomTagWorkflow]):
        await handle.signal(CustomTagWorkflow.kick)
        await handle.result()

    # The RunWorkflow span from the second worker carries the custom tag.
    run = span_collector.one("RunWorkflow")
    assert run.get_tag("custom.workflow.tag") == "hello-from-workflow"


@pytest.mark.asyncio
async def test_manual_keep_root_spans(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """Entry-point spans are force-sampled (USER_KEEP) when they are trace roots.

    StartWorkflow with no active parent and RunWorkflow with a parent from
    Temporal headers both carry sampling_priority == 2, matching Go's
    manualKeepOps behavior. StartWorkflow with an in-process parent inherits
    that parent's sampling decision.
    """
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(
        tc, task_queue=tq, workflows=[TestWorkflow], activities=[echo_activity]
    ):
        await (
            await tc.start_workflow(
                TestWorkflow.run,
                TestRequest(use_activity=False),
                id=f"wf-{uuid.uuid4()}",
                task_queue=tq,
            )
        ).result()

    start = span_collector.one("StartWorkflow")
    run = span_collector.one("RunWorkflow")

    # USER_KEEP == 2: span is force-sampled, overriding the global sample rate.
    assert start.context.sampling_priority == 2, (
        f"StartWorkflow expected sampling_priority=2 (USER_KEEP), got {start.context.sampling_priority}"
    )
    assert run.context.sampling_priority == 2, (
        f"RunWorkflow expected sampling_priority=2 (USER_KEEP), got {run.context.sampling_priority}"
    )


@pytest.mark.asyncio
async def test_manual_keep_not_applied_with_local_active_parent(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """StartWorkflow inherits the caller's sampling decision when there is an
    active local parent span; only RunWorkflow (parent from Temporal headers)
    is force-sampled to USER_KEEP.

    Guards against the regression where unconditional manual.keep on entry-point
    operations promoted the caller's entire trace to USER_KEEP when StartWorkflow
    was called from inside an instrumented HTTP handler.
    """
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    with _dd_tracer.trace("caller.request") as caller_span:
        async with Worker(
            tc, task_queue=tq, workflows=[TestWorkflow], activities=[echo_activity]
        ):
            handle = await tc.start_workflow(
                TestWorkflow.run,
                TestRequest(use_activity=False),
                id=f"wf-{uuid.uuid4()}",
                task_queue=tq,
            )
            await handle.result()

    start_wf = span_collector.one("StartWorkflow")
    run_wf = span_collector.one("RunWorkflow")

    # StartWorkflow is a child of the active caller span — it must not be
    # force-promoted to USER_KEEP, so the caller's sampling decision applies.
    assert start_wf.parent_id == caller_span.span_id
    assert start_wf.context.sampling_priority != 2, (
        f"StartWorkflow must not be USER_KEEP when called with a local active parent, "
        f"got sampling_priority={start_wf.context.sampling_priority}"
    )

    # RunWorkflow's parent arrives via Temporal task headers (cross-process),
    # so it is force-sampled regardless of the caller's sampling decision.
    assert run_wf.context.sampling_priority == 2, (
        f"RunWorkflow expected sampling_priority=2 (USER_KEEP), got {run_wf.context.sampling_priority}"
    )


@pytest.mark.asyncio
async def test_workflow_logger_trace_correlation(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """workflow.logger calls are enriched with dd.trace_id and dd.span_id.

    The _DDTraceLogFilter registered at module import time injects the active
    RunWorkflow span IDs into every log record emitted via workflow.logger,
    enabling Datadog log-to-trace correlation without activating the span.
    """
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    capturer = LogCapturer()
    with capturer.logs_captured(logging.getLogger("temporalio.workflow")):
        async with Worker(tc, task_queue=tq, workflows=[LoggingWorkflow]):
            handle = await tc.start_workflow(
                LoggingWorkflow.run,
                id=f"wf-{uuid.uuid4()}",
                task_queue=tq,
            )
            await handle.result()

    run = span_collector.one("RunWorkflow")

    records = capturer.find_all(
        lambda r: "test log message from workflow" in r.getMessage()
    )
    assert len(records) == 1, (
        f"Expected exactly 1 workflow log record, got {len(records)}"
    )
    record = records[0]

    assert record.__dict__.get("dd.trace_id") == format_trace_id(run.trace_id), (
        f"Expected dd.trace_id={format_trace_id(run.trace_id)}, got {record.__dict__.get('dd.trace_id')}"
    )
    assert record.__dict__.get("dd.span_id") == str(run.span_id), (
        f"Expected dd.span_id={run.span_id}, got {record.__dict__.get('dd.span_id')}"
    )


@pytest.mark.asyncio
async def test_workflow_logger_trace_correlation_64bit(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """workflow.logger uses ddtrace's log-correlation trace-ID format.

    IDs that fit in 64 bits use decimal; larger IDs use 32-character hexadecimal.
    Disabling 128-bit generation exercises the 64-bit path.
    """
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    import ddtrace

    orig = ddtrace.config._128_bit_trace_id_enabled
    ddtrace.config._128_bit_trace_id_enabled = False
    capturer = LogCapturer()
    try:
        interceptor = _make_interceptor()
        tc = _traced_client(client, interceptor)
        tq = _task_queue()

        with capturer.logs_captured(logging.getLogger("temporalio.workflow")):
            async with Worker(tc, task_queue=tq, workflows=[LoggingWorkflow]):
                handle = await tc.start_workflow(
                    LoggingWorkflow.run,
                    id=f"wf-{uuid.uuid4()}",
                    task_queue=tq,
                )
                await handle.result()
    finally:
        ddtrace.config._128_bit_trace_id_enabled = orig

    run = span_collector.one("RunWorkflow")
    assert run.trace_id <= (1 << 64) - 1, "expected a 64-bit trace ID"

    records = capturer.find_all(
        lambda r: "test log message from workflow" in r.getMessage()
    )
    assert len(records) == 1, (
        f"Expected exactly 1 workflow log record, got {len(records)}"
    )
    record = records[0]

    expected = str(
        run.trace_id
    )  # decimal, matching ddtrace's format_trace_id for 64-bit IDs
    assert record.__dict__.get("dd.trace_id") == expected, (
        f"Expected dd.trace_id={expected!r} (decimal), got {record.__dict__.get('dd.trace_id')!r}"
    )


@pytest.mark.asyncio
async def test_activity_logger_trace_correlation(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """activity.logger calls are enriched with dd.trace_id and dd.span_id.

    Unlike the workflow path (which uses the module-level _DDTraceLogFilter),
    activity logs run outside the sandbox with a real active ddtrace span
    (activated with activate=True in the activity interceptor), so correlation
    relies on ddtrace's own stdlib logging patch instead of a custom filter.
    """
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    import ddtrace

    ddtrace.patch(logging=True)

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    capturer = LogCapturer()
    with capturer.logs_captured(logging.getLogger("temporalio.activity")):
        async with Worker(
            tc,
            task_queue=tq,
            workflows=[LoggingActivityWorkflow],
            activities=[logging_activity],
        ):
            handle = await tc.start_workflow(
                LoggingActivityWorkflow.run,
                id=f"wf-{uuid.uuid4()}",
                task_queue=tq,
            )
            await handle.result()

    run_act = span_collector.one("RunActivity")

    records = capturer.find_all(
        lambda r: "test log message from activity" in r.getMessage()
    )
    assert len(records) == 1, (
        f"Expected exactly 1 activity log record, got {len(records)}"
    )
    record = records[0]

    assert record.__dict__.get("dd.trace_id") == format_trace_id(run_act.trace_id), (
        f"Expected dd.trace_id={format_trace_id(run_act.trace_id)}, got {record.__dict__.get('dd.trace_id')}"
    )
    assert record.__dict__.get("dd.span_id") == str(run_act.span_id), (
        f"Expected dd.span_id={run_act.span_id}, got {record.__dict__.get('dd.span_id')}"
    )


@pytest.mark.asyncio
async def test_custom_span_in_activity_is_child_of_run_activity(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """A custom ddtrace span created inside an activity is a child of RunActivity.

    The activity interceptor activates the RunActivity span with activate=True
    so that user-created spans inside the activity automatically inherit the
    correct trace context.
    """
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    async with Worker(
        tc,
        task_queue=tq,
        workflows=[CustomSpanActivityWorkflow],
        activities=[custom_span_activity],
    ):
        handle = await tc.start_workflow(
            CustomSpanActivityWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=tq,
        )
        parent_id, trace_id = await handle.result()

    run_act = span_collector.one("RunActivity")

    assert trace_id == run_act.trace_id, (
        f"Custom span trace_id {trace_id} != RunActivity trace_id {run_act.trace_id} — "
        "custom span is an independent trace, not a child of RunActivity"
    )
    assert parent_id == run_act.span_id, (
        f"Custom span parent_id {parent_id} != RunActivity span_id {run_act.span_id}"
    )


@pytest.mark.asyncio
async def test_custom_span_in_activity_inherits_caller_trace(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """Custom spans in activities are in the same trace as the workflow caller.

    Simulates a caller service that already has an active span when
    start_workflow is called (e.g. an HTTP request handler span). StartWorkflow
    must be a child of that span, and all downstream spans — RunWorkflow,
    StartActivity, RunActivity, and user-created custom spans inside the
    activity — must share the caller's trace_id.
    """
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    with _dd_tracer.trace("caller.request") as caller_span:
        async with Worker(
            tc,
            task_queue=tq,
            workflows=[CustomSpanActivityWorkflow],
            activities=[custom_span_activity],
        ):
            handle = await tc.start_workflow(
                CustomSpanActivityWorkflow.run,
                id=f"wf-{uuid.uuid4()}",
                task_queue=tq,
            )
            custom_parent_id, custom_trace_id = await handle.result()

    start_wf = span_collector.one("StartWorkflow")
    run_act = span_collector.one("RunActivity")

    assert start_wf.trace_id == caller_span.trace_id, (
        f"StartWorkflow trace_id {start_wf.trace_id} != caller trace_id {caller_span.trace_id}"
    )
    assert start_wf.parent_id == caller_span.span_id, (
        f"StartWorkflow parent_id {start_wf.parent_id} != caller span_id {caller_span.span_id}"
    )
    assert run_act.trace_id == caller_span.trace_id, (
        f"RunActivity trace_id {run_act.trace_id} != caller trace_id {caller_span.trace_id} — "
        "trace context did not propagate through Temporal headers"
    )
    assert custom_trace_id == caller_span.trace_id, (
        f"Custom span trace_id {custom_trace_id} != caller trace_id {caller_span.trace_id} — "
        "custom span is not in the caller's trace"
    )
    assert custom_parent_id == run_act.span_id, (
        f"Custom span parent_id {custom_parent_id} != RunActivity span_id {run_act.span_id}"
    )


@pytest.mark.asyncio
async def test_concurrent_workflows_log_isolation(
    client: Client,
    env: WorkflowEnvironment,
    span_collector: _SpanCollector,
) -> None:
    """Two workflows running concurrently on the same worker log their own trace IDs.

    ConcurrentLoggingWorkflow emits logs before and after an activity, giving
    the Temporal SDK a chance to interleave the two executions.  Each log record
    must carry the dd.trace_id/dd.span_id of its own RunWorkflow span, not the
    other workflow's span.
    """
    if env.supports_time_skipping:
        pytest.skip("time-skipping server not supported")

    interceptor = _make_interceptor()
    tc = _traced_client(client, interceptor)
    tq = _task_queue()

    wf_id_a = f"wf-a-{uuid.uuid4()}"
    wf_id_b = f"wf-b-{uuid.uuid4()}"

    capturer = LogCapturer()
    with capturer.logs_captured(logging.getLogger("temporalio.workflow")):
        async with Worker(
            tc,
            task_queue=tq,
            workflows=[ConcurrentLoggingWorkflow],
            activities=[logging_activity],
        ):
            handle_a = await tc.start_workflow(
                ConcurrentLoggingWorkflow.run,
                "A",
                id=wf_id_a,
                task_queue=tq,
            )
            handle_b = await tc.start_workflow(
                ConcurrentLoggingWorkflow.run,
                "B",
                id=wf_id_b,
                task_queue=tq,
            )
            # Wait until both workflows have logged "start:<label>" and are blocked
            # on the signal barrier — this proves they are in-flight concurrently.
            for handle in (handle_a, handle_b):
                for _ in range(50):
                    try:
                        if await handle.query(ConcurrentLoggingWorkflow.is_started):
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(0.05)
            await handle_a.signal(ConcurrentLoggingWorkflow.proceed)
            await handle_b.signal(ConcurrentLoggingWorkflow.proceed)
            await asyncio.gather(handle_a.result(), handle_b.result())

    run_wfs = span_collector.by_op("RunWorkflow")
    assert len(run_wfs) == 2, f"Expected 2 RunWorkflow spans, got {len(run_wfs)}"

    span_by_wf_id = {span_collector.tag(s, "WorkflowID"): s for s in run_wfs}
    span_a = span_by_wf_id[wf_id_a]
    span_b = span_by_wf_id[wf_id_b]

    for label, span in [("A", span_a), ("B", span_b)]:
        expected_trace_id = format_trace_id(span.trace_id)
        expected_span_id = str(span.span_id)

        for marker in (f"start:{label}", f"end:{label}"):
            records = capturer.find_all(lambda r, marker=marker: marker in r.getMessage())
            assert len(records) >= 1, f"Expected at least 1 log record for {marker!r}"
            record = records[0]
            assert record.__dict__.get("dd.trace_id") == expected_trace_id, (
                f"{marker}: expected dd.trace_id={expected_trace_id}, got {record.__dict__.get('dd.trace_id')}"
            )
            assert record.__dict__.get("dd.span_id") == expected_span_id, (
                f"{marker}: expected dd.span_id={expected_span_id}, got {record.__dict__.get('dd.span_id')}"
            )
