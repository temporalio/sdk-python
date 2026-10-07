"""Retired workflow instances must not unregister a replacement during cleanup."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langgraph.graph import StateGraph
from langgraph.pregel import Pregel

from temporalio import workflow
from temporalio.contrib.langgraph import entrypoint, graph
from temporalio.contrib.langgraph import _activity, _interceptor
from temporalio.worker import (
    ExecuteWorkflowInput,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)


@pytest.fixture(autouse=True)
def isolate_registrations():
    yield
    _interceptor._workflow_graphs.clear()
    _interceptor._workflow_entrypoints.clear()
    _activity._warned_store_runs.clear()


def plugin():
    return _interceptor.LangGraphInterceptor(
        {"graph": Mock(spec=StateGraph)}, {"entrypoint": Mock(spec=Pregel)}
    )


def start(interceptor, run_id):
    async def suspend(_input):
        await asyncio.sleep(0)
        return "done"

    next_interceptor = Mock(spec=WorkflowInboundInterceptor)
    next_interceptor.execute_workflow = AsyncMock(side_effect=suspend)
    inbound = interceptor.workflow_interceptor_class(
        Mock(spec=WorkflowInterceptorClassInput)
    )(next_interceptor)
    outbound = Mock(spec=WorkflowOutboundInterceptor)
    outbound.info.return_value = SimpleNamespace(run_id=run_id)
    inbound.init(outbound)
    execution = inbound.execute_workflow(Mock(spec=ExecuteWorkflowInput))
    execution.send(None)
    return execution


@pytest.mark.parametrize("reuse_plugin", [False, True])
def test_retired_instance_preserves_replacement(monkeypatch, reuse_plugin):
    monkeypatch.setattr(workflow, "info", lambda: SimpleNamespace(run_id="same-run"))
    first_plugin = plugin()
    second_plugin = first_plugin if reuse_plugin else plugin()
    retired = start(first_plugin, "same-run")
    replacement = start(second_plugin, "same-run")
    _activity._warned_store_runs.add("same-run")
    try:
        retired.close()
        assert graph("graph") is second_plugin._graphs["graph"]
        assert entrypoint("entrypoint") is second_plugin._entrypoints["entrypoint"]
        assert "same-run" in _activity._warned_store_runs
    finally:
        retired.close()
        replacement.close()
    with pytest.raises(RuntimeError, match="graph\\(\\) must"):
        graph("graph")
    assert "same-run" not in _activity._warned_store_runs


def test_cleanup_uses_original_run_when_collected_in_another_workflow(monkeypatch):
    monkeypatch.setattr(workflow, "info", lambda: SimpleNamespace(run_id="first-run"))
    first = start(plugin(), "first-run")
    second_plugin = plugin()
    second = start(second_plugin, "second-run")
    monkeypatch.setattr(workflow, "info", lambda: SimpleNamespace(run_id="second-run"))
    try:
        first.close()
        assert graph("graph") is second_plugin._graphs["graph"]
        monkeypatch.setattr(
            workflow, "info", lambda: SimpleNamespace(run_id="first-run")
        )
        with pytest.raises(RuntimeError, match="graph\\(\\) must"):
            graph("graph")
    finally:
        first.close()
        monkeypatch.setattr(
            workflow, "info", lambda: SimpleNamespace(run_id="second-run")
        )
        second.close()


def test_cleanup_does_not_require_workflow_context(monkeypatch):
    execution = start(plugin(), "run")
    monkeypatch.setattr(workflow, "info", Mock(side_effect=RuntimeError("no context")))
    execution.close()
    monkeypatch.setattr(workflow, "info", lambda: SimpleNamespace(run_id="run"))
    with pytest.raises(RuntimeError, match="graph\\(\\) must"):
        graph("graph")


def test_completed_execution_releases_registration(monkeypatch):
    monkeypatch.setattr(workflow, "info", lambda: SimpleNamespace(run_id="run"))
    execution = start(plugin(), "run")
    with pytest.raises(StopIteration, match="done"):
        execution.send(None)
    with pytest.raises(RuntimeError, match="graph\\(\\) must"):
        graph("graph")
