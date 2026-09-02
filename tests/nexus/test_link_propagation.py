"""Unit tests for Nexus link propagation.

These exercise link propagation when a Nexus operation handler queries or signals a
workflow, or starts a workflow, workflow update, or activity against a mocked workflow
service. End-to-end signal backlinks require a server with
EnableCHASMSignalBacklinks enabled and are not covered here.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import timedelta
from typing import Any
from unittest import mock

import nexusrpc
import nexusrpc.handler
import pytest
from nexusrpc.handler import (
    OperationHandler,
    StartOperationContext,
    StartOperationResultAsync,
    service_handler,
    sync_operation,
)
from nexusrpc.handler._decorators import operation_handler

import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.api.nexus.v1
import temporalio.api.update.v1
import temporalio.api.workflowservice.v1
import temporalio.client
import temporalio.common
import temporalio.converter
import temporalio.nexus._link_conversion
import temporalio.nexus._operation_context
import temporalio.nexus._token
from temporalio.client._impl import _ClientImpl
from temporalio.client._interceptor import (
    QueryWorkflowInput,
    SignalWorkflowInput,
    StartActivityInput,
    StartWorkflowInput,
    StartWorkflowUpdateInput,
    UpdateWithStartUpdateWorkflowInput,
)
from temporalio.nexus._operation_context import _TemporalStartOperationContext
from temporalio.worker._nexus import _NexusTaskCancellation, _NexusWorker

NAMESPACE = "test-namespace"
WORKFLOW_ID = "wf-target"


def _workflow_event_link(
    workflow_id: str,
    run_id: str,
    event_type: temporalio.api.enums.v1.EventType.ValueType,
) -> temporalio.api.common.v1.Link:
    return temporalio.api.common.v1.Link(
        workflow_event=temporalio.api.common.v1.Link.WorkflowEvent(
            namespace=NAMESPACE,
            workflow_id=workflow_id,
            run_id=run_id,
            event_ref=temporalio.api.common.v1.Link.WorkflowEvent.EventReference(
                event_type=event_type,
            ),
        )
    )


def _workflow_link(
    workflow_id: str, run_id: str, *, reason: str
) -> temporalio.api.common.v1.Link:
    return temporalio.api.common.v1.Link(
        workflow=temporalio.api.common.v1.Link.Workflow(
            namespace=NAMESPACE,
            workflow_id=workflow_id,
            run_id=run_id,
            reason=reason,
        )
    )


def _inbound_nexus_link() -> temporalio.api.common.v1.Link:
    return _workflow_event_link(
        "caller-wf",
        "caller-run",
        temporalio.api.enums.v1.EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED,
    )


@pytest.fixture
def nexus_ctx() -> Generator[_TemporalStartOperationContext]:
    """Install a Nexus start-operation context with a single inbound link.

    The inbound link is provided in nexusrpc.Link form, exactly as the worker populates it from
    the inbound Nexus task. Yields the temporal context so tests can inspect outbound_links.
    """
    inbound = temporalio.nexus._link_conversion.workflow_event_to_nexus_link(
        _inbound_nexus_link().workflow_event
    )
    nexus_context = nexusrpc.handler.StartOperationContext(
        service="svc",
        operation="op",
        headers={},
        request_id="req-id",
        callback_url="https://callback.example",
        inbound_links=[inbound],
        callback_headers={"callback-header": "value"},
        task_cancellation=_NexusTaskCancellation(),
    )
    ctx = temporalio.nexus._operation_context._TemporalStartOperationContext(
        nexus_context=nexus_context,
        client=mock.MagicMock(namespace=NAMESPACE),
        info=lambda: temporalio.nexus.Info(
            endpoint="endpoint", namespace=NAMESPACE, task_queue="tq"
        ),
        _runtime_metric_meter=mock.MagicMock(),
        _worker_shutdown_event=mock.MagicMock(),
    )
    token = temporalio.nexus._operation_context._temporal_start_operation_context.set(
        ctx
    )
    try:
        yield ctx
    finally:
        temporalio.nexus._operation_context._temporal_start_operation_context.reset(
            token
        )


@pytest.fixture
def nexus_ctx_linkless() -> Generator[_TemporalStartOperationContext]:
    """Like `nexus_ctx`, but with no inbound links and no callback URL."""
    nexus_context = nexusrpc.handler.StartOperationContext(
        service="svc",
        operation="op",
        headers={},
        request_id="req-id",
        callback_url=None,
        inbound_links=[],
        callback_headers={},
        task_cancellation=_NexusTaskCancellation(),
    )
    ctx = temporalio.nexus._operation_context._TemporalStartOperationContext(
        nexus_context=nexus_context,
        client=mock.MagicMock(namespace=NAMESPACE),
        info=lambda: temporalio.nexus.Info(
            endpoint="endpoint", namespace=NAMESPACE, task_queue="tq"
        ),
        _runtime_metric_meter=mock.MagicMock(),
        _worker_shutdown_event=mock.MagicMock(),
    )
    token = temporalio.nexus._operation_context._temporal_start_operation_context.set(
        ctx
    )
    try:
        yield ctx
    finally:
        temporalio.nexus._operation_context._temporal_start_operation_context.reset(
            token
        )


def _make_client_impl(workflow_service: Any) -> _ClientImpl:
    client = mock.MagicMock()
    client.namespace = NAMESPACE
    client.identity = "test-identity"
    client.workflow_service = workflow_service
    client.data_converter = temporalio.converter.DataConverter.default
    return _ClientImpl(client)


def _signal_input() -> SignalWorkflowInput:
    return SignalWorkflowInput(
        id=WORKFLOW_ID,
        run_id=None,
        signal="test-signal",
        args=[],
        headers={},
        rpc_metadata={},
        rpc_timeout=None,
    )


def _query_input() -> QueryWorkflowInput:
    return QueryWorkflowInput(
        id=WORKFLOW_ID,
        run_id=None,
        query="query-done",
        args=[],
        reject_condition=None,
        headers={},
        ret_type=bool,
        rpc_metadata={},
        rpc_timeout=None,
    )


def _start_input(start_signal: str | None = None) -> StartWorkflowInput:
    return StartWorkflowInput(
        workflow="TestWorkflow",
        args=[],
        id=WORKFLOW_ID,
        task_queue="tq",
        execution_timeout=None,
        run_timeout=None,
        task_timeout=None,
        id_reuse_policy=temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy=temporalio.common.WorkflowIDConflictPolicy.UNSPECIFIED,
        retry_policy=None,
        cron_schedule="",
        memo=None,
        search_attributes=None,
        start_delay=None,
        headers={},
        start_signal=start_signal,
        start_signal_args=[],
        static_summary=None,
        static_details=None,
        ret_type=None,
        rpc_metadata={},
        rpc_timeout=None,
        request_eager_start=False,
        priority=temporalio.common.Priority.default,
        versioning_override=None,
    )


def _start_activity_input() -> StartActivityInput:
    return StartActivityInput(
        activity_type="TestActivity",
        args=[],
        id="activity-target",
        task_queue="tq",
        result_type=None,
        schedule_to_close_timeout=None,
        start_to_close_timeout=timedelta(seconds=10),
        schedule_to_start_timeout=None,
        heartbeat_timeout=None,
        id_reuse_policy=temporalio.common.ActivityIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy=temporalio.common.ActivityIDConflictPolicy.FAIL,
        retry_policy=None,
        priority=temporalio.common.Priority.default,
        search_attributes=None,
        summary=None,
        start_delay=None,
        headers={},
        rpc_metadata={},
        rpc_timeout=None,
    )


def _start_workflow_update_input() -> StartWorkflowUpdateInput:
    return StartWorkflowUpdateInput(
        id=WORKFLOW_ID,
        run_id="target-run",
        first_execution_run_id=None,
        update_id="update-id",
        update="test-update",
        args=[],
        wait_for_stage=temporalio.client.WorkflowUpdateStage.ACCEPTED,
        headers={},
        ret_type=None,
        rpc_metadata={},
        rpc_timeout=None,
    )


def _update_with_start_update_input() -> UpdateWithStartUpdateWorkflowInput:
    return UpdateWithStartUpdateWorkflowInput(
        update_id="update-id",
        update="test-update",
        args=[],
        wait_for_stage=temporalio.client.WorkflowUpdateStage.ACCEPTED,
        headers={},
        ret_type=None,
    )


def _outbound_link_urls(ctx: Any) -> list[str]:
    return [link.url for link in ctx.nexus_context.outbound_links]


def test_response_link_captures_workflow_link(
    nexus_ctx: _TemporalStartOperationContext,
) -> None:
    nexus_ctx._add_response_link(
        _workflow_link(WORKFLOW_ID, "target-run", reason="Query processed")
    )

    assert nexus_ctx.nexus_context.outbound_links == [
        nexusrpc.Link(
            type=temporalio.api.common.v1.Link.Workflow.DESCRIPTOR.full_name,
            url=(
                "temporal:///namespaces/test-namespace/workflows/"
                "wf-target/target-run?reason=Query+processed"
            ),
        )
    ]


# ── signal ────────────────────────────────────────────────────────────────────────────────


# Query responses differ from Signal responses by linking to the Workflow rather than an event.
async def test_query_captures_response_workflow_link(
    nexus_ctx: _TemporalStartOperationContext,
) -> None:
    payloads = await temporalio.converter.DataConverter.default.encode([False])
    workflow_service = mock.MagicMock()
    workflow_service.query_workflow = mock.AsyncMock(
        return_value=temporalio.api.workflowservice.v1.QueryWorkflowResponse(
            query_result=temporalio.api.common.v1.Payloads(payloads=payloads),
            link=_workflow_link(WORKFLOW_ID, "target-run", reason="Query processed"),
        )
    )
    impl = _make_client_impl(workflow_service)

    result = await impl.query_workflow(_query_input())

    assert result is False
    assert nexus_ctx.nexus_context.outbound_links == [
        nexusrpc.Link(
            type=temporalio.api.common.v1.Link.Workflow.DESCRIPTOR.full_name,
            url=(
                "temporal:///namespaces/test-namespace/workflows/"
                "wf-target/target-run?reason=Query+processed"
            ),
        )
    ]


# Signal responses link to the event that accepted the Signal.
async def test_signal_forwards_inbound_links_and_captures_response_backlink(
    nexus_ctx: _TemporalStartOperationContext,
) -> None:
    response_link = _workflow_event_link(
        WORKFLOW_ID,
        "target-run",
        temporalio.api.enums.v1.EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED,
    )
    workflow_service = mock.MagicMock()
    workflow_service.signal_workflow_execution = mock.AsyncMock(
        return_value=temporalio.api.workflowservice.v1.SignalWorkflowExecutionResponse(
            link=response_link
        )
    )
    impl = _make_client_impl(workflow_service)

    await impl.signal_workflow(_signal_input())

    # Forward: the request carries the single inbound link.
    sent = workflow_service.signal_workflow_execution.call_args.args[0]
    assert len(sent.links) == 1
    assert sent.links[0] == _inbound_nexus_link()

    # Backward: the response link is added to the operation's outbound links (as a Nexus link).
    assert len(nexus_ctx.nexus_context.outbound_links) == 1
    assert "wf-target" in _outbound_link_urls(nexus_ctx)[0]


async def test_signal_against_older_server_captures_no_backlink(
    nexus_ctx: _TemporalStartOperationContext,
) -> None:
    workflow_service = mock.MagicMock()
    workflow_service.signal_workflow_execution = mock.AsyncMock(
        return_value=temporalio.api.workflowservice.v1.SignalWorkflowExecutionResponse()
    )
    impl = _make_client_impl(workflow_service)

    await impl.signal_workflow(_signal_input())

    # Forward direction still works regardless of server version.
    sent = workflow_service.signal_workflow_execution.call_args.args[0]
    assert len(sent.links) == 1

    # Backward: no backlink because the server returned no link.
    assert nexus_ctx.nexus_context.outbound_links == []


async def test_multiple_signals_accumulate_all_backlinks(
    nexus_ctx: _TemporalStartOperationContext,
) -> None:
    first = _workflow_event_link(
        "callee-a",
        "run-a",
        temporalio.api.enums.v1.EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED,
    )
    second = _workflow_event_link(
        "callee-b",
        "run-b",
        temporalio.api.enums.v1.EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED,
    )
    workflow_service = mock.MagicMock()
    workflow_service.signal_workflow_execution = mock.AsyncMock(
        side_effect=[
            temporalio.api.workflowservice.v1.SignalWorkflowExecutionResponse(
                link=first
            ),
            temporalio.api.workflowservice.v1.SignalWorkflowExecutionResponse(
                link=second
            ),
        ]
    )
    impl = _make_client_impl(workflow_service)

    await impl.signal_workflow(_signal_input())
    await impl.signal_workflow(_signal_input())

    urls = _outbound_link_urls(nexus_ctx)
    assert len(urls) == 2
    assert "callee-a" in urls[0]
    assert "callee-b" in urls[1]


async def test_signal_outside_nexus_context_does_not_touch_links() -> None:
    workflow_service = mock.MagicMock()
    workflow_service.signal_workflow_execution = mock.AsyncMock(
        return_value=temporalio.api.workflowservice.v1.SignalWorkflowExecutionResponse()
    )
    impl = _make_client_impl(workflow_service)

    await impl.signal_workflow(_signal_input())

    sent = workflow_service.signal_workflow_execution.call_args.args[0]
    assert len(sent.links) == 0


# ── signal-with-start ───────────────────────────────────────────────────────────────────────


async def test_signal_with_start_forwards_inbound_links_and_captures_backlink(
    nexus_ctx: _TemporalStartOperationContext,
) -> None:
    response_link = _workflow_event_link(
        WORKFLOW_ID,
        "target-run",
        temporalio.api.enums.v1.EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED,
    )
    workflow_service = mock.MagicMock()
    workflow_service.signal_with_start_workflow_execution = mock.AsyncMock(
        return_value=temporalio.api.workflowservice.v1.SignalWithStartWorkflowExecutionResponse(
            run_id="target-run",
            signal_link=response_link,
        )
    )
    impl = _make_client_impl(workflow_service)

    await impl.start_workflow(_start_input(start_signal="test-signal"))

    # Forward: the SignalWithStart request carries the inbound link.
    sent = workflow_service.signal_with_start_workflow_execution.call_args.args[0]
    assert len(sent.links) == 1
    assert sent.links[0] == _inbound_nexus_link()

    # Backward: response.signal_link is captured as an outbound Nexus link.
    assert len(nexus_ctx.nexus_context.outbound_links) == 1
    assert "wf-target" in _outbound_link_urls(nexus_ctx)[0]


async def test_signal_with_start_against_older_server_captures_no_backlink(
    nexus_ctx: _TemporalStartOperationContext,
) -> None:
    workflow_service = mock.MagicMock()
    workflow_service.signal_with_start_workflow_execution = mock.AsyncMock(
        return_value=temporalio.api.workflowservice.v1.SignalWithStartWorkflowExecutionResponse(
            run_id="target-run",
        )
    )
    impl = _make_client_impl(workflow_service)

    await impl.start_workflow(_start_input(start_signal="test-signal"))

    sent = workflow_service.signal_with_start_workflow_execution.call_args.args[0]
    assert len(sent.links) == 1
    assert nexus_ctx.nexus_context.outbound_links == []


# ── start ─────────────────────────────────────────────────────────────────────────────────


async def test_start_forwards_inbound_links_and_captures_backlink(
    nexus_ctx: _TemporalStartOperationContext,
) -> None:
    server_link = _workflow_event_link(
        WORKFLOW_ID,
        "target-run",
        temporalio.api.enums.v1.EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED,
    )
    workflow_service = mock.MagicMock()
    workflow_service.start_workflow_execution = mock.AsyncMock(
        return_value=temporalio.api.workflowservice.v1.StartWorkflowExecutionResponse(
            run_id="target-run",
            link=server_link,
        )
    )
    impl = _make_client_impl(workflow_service)

    await impl.start_workflow(_start_input())

    # Forward: the start request carries the single inbound link.
    sent = workflow_service.start_workflow_execution.call_args.args[0]
    assert len(sent.links) == 1
    assert sent.links[0] == _inbound_nexus_link()

    # Backward: a plain start captures a backlink
    assert len(nexus_ctx.nexus_context.outbound_links) == 1
    assert "wf-target" in _outbound_link_urls(nexus_ctx)[0]


async def test_start_against_older_server_captures_no_backlink(
    nexus_ctx: _TemporalStartOperationContext,
) -> None:
    workflow_service = mock.MagicMock()
    workflow_service.start_workflow_execution = mock.AsyncMock(
        return_value=temporalio.api.workflowservice.v1.StartWorkflowExecutionResponse(
            run_id="target-run",
        )
    )
    impl = _make_client_impl(workflow_service)

    await impl.start_workflow(_start_input())

    # Forward direction still works regardless of server version.
    sent = workflow_service.start_workflow_execution.call_args.args[0]
    assert len(sent.links) == 1
    assert sent.links[0] == _inbound_nexus_link()

    # Backward: a plain start fabricates a backlink when the server doesn't return one.
    assert len(nexus_ctx.nexus_context.outbound_links) == 1
    assert "wf-target" in _outbound_link_urls(nexus_ctx)[0]


async def test_start_outside_nexus_context_does_not_touch_links() -> None:
    workflow_service = mock.MagicMock()
    workflow_service.start_workflow_execution = mock.AsyncMock(
        return_value=temporalio.api.workflowservice.v1.StartWorkflowExecutionResponse(
            run_id="target-run",
        )
    )
    impl = _make_client_impl(workflow_service)

    # Should not raise even though there is no Nexus context.
    handle = await impl.start_workflow(_start_input())
    assert handle.result_run_id == "target-run"
    sent = workflow_service.start_workflow_execution.call_args.args[0]
    assert len(sent.links) == 0


# ── start: on-conflict options ──────────────────────────────────────────────────────────────
#
# A start issued from inside a Nexus operation handler enables all on_conflict_options so that,
# under a WORKFLOW_ID_CONFLICT_POLICY_USE_EXISTING policy, a detected conflict attaches the
# request id, completion callbacks, and links to the existing run.


def _start_response() -> (
    temporalio.api.workflowservice.v1.StartWorkflowExecutionResponse
):
    return temporalio.api.workflowservice.v1.StartWorkflowExecutionResponse(
        run_id="target-run",
    )


@pytest.mark.usefixtures("nexus_ctx")
async def test_start_from_nexus_context_sets_all_on_conflict_options() -> None:
    workflow_service = mock.MagicMock()
    workflow_service.start_workflow_execution = mock.AsyncMock(
        return_value=_start_response()
    )
    impl = _make_client_impl(workflow_service)

    await impl.start_workflow(_start_input())

    sent = workflow_service.start_workflow_execution.call_args.args[0]
    assert sent.HasField("on_conflict_options")
    assert sent.on_conflict_options.attach_request_id
    assert sent.on_conflict_options.attach_completion_callbacks
    assert sent.on_conflict_options.attach_links
    assert sent.request_id
    assert sent.request_id != "req-id"
    assert len(sent.completion_callbacks) == 0


@pytest.mark.usefixtures("nexus_ctx")
async def test_backing_workflow_start_gets_nexus_request_fields() -> None:
    workflow_service = mock.MagicMock()
    workflow_service.start_workflow_execution = mock.AsyncMock(
        return_value=_start_response()
    )
    impl = _make_client_impl(workflow_service)

    with temporalio.nexus._operation_context._nexus_backing_start_context():
        await impl.start_workflow(_start_input())

    sent = workflow_service.start_workflow_execution.call_args.args[0]
    assert sent.HasField("on_conflict_options")
    assert sent.on_conflict_options.attach_request_id
    assert sent.on_conflict_options.attach_completion_callbacks
    assert sent.on_conflict_options.attach_links
    assert len(sent.links) == 1
    assert sent.links[0] == _inbound_nexus_link()
    assert sent.request_id == "req-id"
    assert len(sent.completion_callbacks) == 1
    operation_token = temporalio.nexus._token.OperationToken.decode(
        sent.completion_callbacks[0].nexus.header["nexus-operation-token"]
    )
    assert operation_token.type is temporalio.nexus._token.OperationTokenType.WORKFLOW
    assert operation_token.namespace == NAMESPACE
    assert operation_token.workflow_id == WORKFLOW_ID
    assert list(sent.completion_callbacks[0].links) == [_inbound_nexus_link()]


async def test_start_outside_nexus_context_leaves_on_conflict_options_unset() -> None:
    workflow_service = mock.MagicMock()
    workflow_service.start_workflow_execution = mock.AsyncMock(
        return_value=_start_response()
    )
    impl = _make_client_impl(workflow_service)

    await impl.start_workflow(_start_input())

    sent = workflow_service.start_workflow_execution.call_args.args[0]
    assert not sent.HasField("on_conflict_options")


# ── workflow update ──────────────────────────────────────────────────────────────


def _workflow_update_response(
    link: temporalio.api.common.v1.Link | None = None,
) -> temporalio.api.workflowservice.v1.UpdateWorkflowExecutionResponse:
    response = temporalio.api.workflowservice.v1.UpdateWorkflowExecutionResponse(
        update_ref=temporalio.api.update.v1.UpdateRef(
            workflow_execution=temporalio.api.common.v1.WorkflowExecution(
                workflow_id=WORKFLOW_ID,
                run_id="target-run",
            ),
            update_id="update-id",
        ),
        stage=temporalio.api.enums.v1.UpdateWorkflowExecutionLifecycleStage.UPDATE_WORKFLOW_EXECUTION_LIFECYCLE_STAGE_ACCEPTED,
    )
    if link is not None:
        response.link.CopyFrom(link)
    return response


async def test_backing_workflow_update_gets_nexus_request_fields_and_backlink(
    nexus_ctx: _TemporalStartOperationContext,
) -> None:
    response_link = _workflow_event_link(
        WORKFLOW_ID,
        "target-run",
        temporalio.api.enums.v1.EventType.EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED,
    )
    workflow_service = mock.MagicMock()
    workflow_service.update_workflow_execution = mock.AsyncMock(
        return_value=_workflow_update_response(response_link)
    )
    impl = _make_client_impl(workflow_service)

    with temporalio.nexus._operation_context._nexus_backing_start_context():
        handle = await impl.start_workflow_update(_start_workflow_update_input())

    assert handle.id == "update-id"
    assert handle.workflow_id == WORKFLOW_ID
    assert handle.workflow_run_id == "target-run"

    sent = workflow_service.update_workflow_execution.call_args.args[0]
    assert sent.request.request_id == "req-id"
    assert list(sent.request.links) == [_inbound_nexus_link()]
    assert len(sent.request.completion_callbacks) == 1
    callback = sent.request.completion_callbacks[0]
    assert callback.nexus.url == "https://callback.example"
    assert callback.nexus.header["callback-header"] == "value"
    operation_token = temporalio.nexus._token.OperationToken.decode(
        callback.nexus.header["nexus-operation-token"]
    )
    assert (
        operation_token.type
        is temporalio.nexus._token.OperationTokenType.UPDATE_WORKFLOW
    )
    assert operation_token.namespace == NAMESPACE
    assert operation_token.workflow_id == WORKFLOW_ID
    assert operation_token.update_id == "update-id"
    assert operation_token.run_id == "target-run"
    assert list(callback.links) == [_inbound_nexus_link()]

    assert len(nexus_ctx.nexus_context.outbound_links) == 1
    assert WORKFLOW_ID in _outbound_link_urls(nexus_ctx)[0]


async def test_non_backing_workflow_update_does_not_get_nexus_request_fields(
    nexus_ctx: _TemporalStartOperationContext,
) -> None:
    workflow_service = mock.MagicMock()
    workflow_service.update_workflow_execution = mock.AsyncMock(
        return_value=_workflow_update_response()
    )
    impl = _make_client_impl(workflow_service)

    await impl.start_workflow_update(_start_workflow_update_input())

    sent = workflow_service.update_workflow_execution.call_args.args[0]
    assert not sent.request.request_id
    assert len(sent.request.links) == 0
    assert len(sent.request.completion_callbacks) == 0
    assert nexus_ctx.nexus_context.outbound_links == []


@pytest.mark.usefixtures("nexus_ctx")
async def test_update_with_start_does_not_get_nexus_request_fields() -> None:
    impl = _make_client_impl(mock.MagicMock())

    with temporalio.nexus._operation_context._nexus_backing_start_context():
        req = await impl._build_update_workflow_execution_request(
            _update_with_start_update_input(), WORKFLOW_ID
        )

    assert not req.request.request_id
    assert len(req.request.links) == 0
    assert len(req.request.completion_callbacks) == 0


# ── activity start ─────────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("nexus_ctx")
async def test_activity_start_forwards_inbound_links() -> None:
    impl = _make_client_impl(mock.MagicMock())

    req = await impl._build_start_activity_execution_request(_start_activity_input())

    assert len(req.links) == 1
    assert req.links[0] == _inbound_nexus_link()
    assert req.request_id == "req-id"
    assert len(req.completion_callbacks) == 0


async def test_activity_start_without_server_link_synthesizes_backlink(
    nexus_ctx: _TemporalStartOperationContext,
) -> None:
    workflow_service = mock.MagicMock()
    workflow_service.start_activity_execution = mock.AsyncMock(
        return_value=temporalio.api.workflowservice.v1.StartActivityExecutionResponse(
            run_id="activity-run",
        )
    )
    impl = _make_client_impl(workflow_service)

    await impl.start_activity(_start_activity_input())

    assert len(nexus_ctx.nexus_context.outbound_links) == 1
    link = nexus_ctx.nexus_context.outbound_links[0]
    assert link.url == (
        "temporal:///namespaces/test-namespace/"
        "activities/activity-target/activity-run/details"
    )
    assert link.type == temporalio.api.common.v1.Link.Activity.DESCRIPTOR.full_name


@pytest.mark.usefixtures("nexus_ctx")
async def test_backing_activity_start_gets_nexus_request_fields() -> None:
    impl = _make_client_impl(mock.MagicMock())

    with temporalio.nexus._operation_context._nexus_backing_start_context():
        req = await impl._build_start_activity_execution_request(
            _start_activity_input()
        )

    assert len(req.links) == 0
    assert req.request_id == "req-id"
    assert len(req.completion_callbacks) == 1
    operation_token = temporalio.nexus._token.OperationToken.decode(
        req.completion_callbacks[0].nexus.header["nexus-operation-token"]
    )
    assert operation_token.type is temporalio.nexus._token.OperationTokenType.ACTIVITY
    assert operation_token.namespace == NAMESPACE
    assert operation_token.activity_id == "activity-target"
    assert list(req.completion_callbacks[0].links) == [_inbound_nexus_link()]


@pytest.mark.usefixtures("nexus_ctx_linkless")
async def test_activity_start_omits_on_conflict_options_when_linkless() -> None:
    impl = _make_client_impl(mock.MagicMock())

    req = await impl._build_start_activity_execution_request(_start_activity_input())

    assert req.request_id == "req-id"
    assert not req.HasField("on_conflict_options")


@pytest.mark.usefixtures("nexus_ctx_linkless")
async def test_backing_activity_start_omits_on_conflict_options_when_linkless() -> None:
    impl = _make_client_impl(mock.MagicMock())

    with temporalio.nexus._operation_context._nexus_backing_start_context():
        req = await impl._build_start_activity_execution_request(
            _start_activity_input()
        )

    assert req.request_id == "req-id"
    assert len(req.completion_callbacks) == 0
    assert not req.HasField("on_conflict_options")


# ── handler-level: backlinks land on the StartOperationResponse ──────────────────────────────

# A response link that a handler stashes on ctx.outbound_links, mimicking what a signal RPC inside
# the handler would do via _add_response_link.
_BACKLINK = temporalio.nexus._link_conversion.workflow_event_to_nexus_link(
    temporalio.api.common.v1.Link.WorkflowEvent(
        namespace=NAMESPACE,
        workflow_id="callee-wf",
        run_id="callee-run-id",
        event_ref=temporalio.api.common.v1.Link.WorkflowEvent.EventReference(
            event_type=temporalio.api.enums.v1.EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED,
        ),
    )
)


class _AsyncBacklinkOperation(OperationHandler):
    """Stashes a backlink then returns an async result, simulating a signaling handler."""

    async def start(
        self, ctx: StartOperationContext, input: str
    ) -> StartOperationResultAsync:
        ctx.outbound_links.append(_BACKLINK)
        return StartOperationResultAsync(token=input)

    async def cancel(self, ctx: Any, token: str) -> None: ...


@service_handler
class _BacklinkStashingService:
    @sync_operation
    async def sync_op(self, ctx: StartOperationContext, _input: str) -> str:
        # Stash a backlink and return a sync result.
        ctx.outbound_links.append(_BACKLINK)
        return "result"

    @operation_handler
    def async_op(self) -> OperationHandler[str, str]:
        return _AsyncBacklinkOperation()


def _make_nexus_worker() -> _NexusWorker:
    return _NexusWorker(
        bridge_worker=lambda: mock.MagicMock(),
        client=mock.MagicMock(namespace=NAMESPACE),
        namespace=NAMESPACE,
        task_queue="tq",
        service_handlers=[_BacklinkStashingService()],
        data_converter=temporalio.converter.DataConverter.default,
        interceptors=[],
        metric_meter=mock.MagicMock(),
        executor=None,
    )


def _start_request(
    operation: str, input: str
) -> temporalio.api.nexus.v1.StartOperationRequest:
    [payload] = (
        temporalio.converter.DataConverter.default.payload_converter.to_payloads(
            [input]
        )
    )
    return temporalio.api.nexus.v1.StartOperationRequest(
        service="_BacklinkStashingService",
        operation=operation,
        payload=payload,
    )


async def test_sync_response_includes_signal_backlinks() -> None:
    worker = _make_nexus_worker()
    response = await worker._start_operation(
        _start_request("sync_op", "input"),
        headers={},
        cancellation=_NexusTaskCancellation(),
        request_deadline=None,
        endpoint="endpoint",
    )
    assert response.HasField("sync_success")
    assert len(response.sync_success.links) == 1
    assert "callee-wf" in response.sync_success.links[0].url


async def test_async_response_includes_signal_backlinks() -> None:
    worker = _make_nexus_worker()
    response = await worker._start_operation(
        _start_request("async_op", "op-token"),
        headers={},
        cancellation=_NexusTaskCancellation(),
        request_deadline=None,
        endpoint="endpoint",
    )
    assert response.HasField("async_success")
    assert response.async_success.operation_token == "op-token"
    assert len(response.async_success.links) == 1
    assert "callee-wf" in response.async_success.links[0].url
