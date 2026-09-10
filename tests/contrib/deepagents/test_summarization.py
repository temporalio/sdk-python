"""Summarization middleware's model resolves through the durable seam.

String summarizer models used to bypass the plugin entirely:
``SummarizationMiddleware`` delegates to LangChain's summarization middleware,
whose ``__init__`` resolves a name string via ``init_chat_model`` (bound at
the top of ``langchain.agents.middleware.summarization``), and
``create_summarization_tool_middleware`` resolves via a call-time ``from
deepagents._models import resolve_model``. Neither reads the
``deepagents.graph`` binding the plugin patched, so a middleware constructed
in-workflow with a name string built a real provider client and ran
compaction LLM calls inside the workflow — nondeterministic, replay-unsafe,
and invisible until a conversation grew past its trigger. (The DEFAULT
stack's summarizer is unaffected: ``create_deep_agent`` resolves the agent
model through the patched graph seam first and hands the middleware the
already-durable instance.) The plugin now patches all three bindings; these
tests pin the resolved type for both explicit string-model paths.
"""

from __future__ import annotations

import sys
import uuid

import pytest

from temporalio.testing import WorkflowEnvironment

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="deepagents requires Python >= 3.11"
)
pytest.importorskip("deepagents")
pytest.importorskip("langchain_core")

from temporalio import workflow
from temporalio.contrib.deepagents import DeepAgentsPlugin, TemporalModel
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from deepagents.backends import StateBackend
    from deepagents.middleware import SummarizationMiddleware
    from deepagents.middleware.summarization import create_summarization_tool_middleware


@workflow.defn
class SummarizerResolutionWorkflow:
    @workflow.run
    async def run(self) -> str:
        # The seam itself, pinned directly: in-workflow, the middleware's
        # resolved summarizer must be a TemporalModel, not a provider client.
        middleware = SummarizationMiddleware(
            "anthropic:claude-sonnet-4-5",
            backend=StateBackend(),
        )
        return type(middleware.model).__name__


@pytest.mark.asyncio
async def test_summarizer_model_resolves_durable(env: WorkflowEnvironment) -> None:
    plugin = DeepAgentsPlugin()
    async with Worker(
        env.client,
        task_queue="da-summarizer-resolve",
        workflows=[SummarizerResolutionWorkflow],
        plugins=[plugin],
    ):
        out = await env.client.execute_workflow(
            SummarizerResolutionWorkflow.run,
            id=f"da-summarizer-resolve-{uuid.uuid4()}",
            task_queue="da-summarizer-resolve",
        )
    assert out == TemporalModel.__name__, out


@workflow.defn
class ToolMiddlewareResolutionWorkflow:
    @workflow.run
    async def run(self) -> str:
        # The OTHER seam: create_summarization_tool_middleware resolves via a
        # call-time `from deepagents._models import resolve_model` — the
        # definition-site binding, which only this patch covers. Its own
        # docstring example passes a name string, so this is a documented
        # user path. The composed middleware keeps the resolved summarizer at
        # `_summarization.model`.
        middleware = create_summarization_tool_middleware(
            "anthropic:claude-sonnet-4-5",
            StateBackend(),
        )
        return type(middleware._summarization.model).__name__


@pytest.mark.asyncio
async def test_tool_middleware_summarizer_resolves_durable(
    env: WorkflowEnvironment,
) -> None:
    plugin = DeepAgentsPlugin()
    async with Worker(
        env.client,
        task_queue="da-summarizer-tool-resolve",
        workflows=[ToolMiddlewareResolutionWorkflow],
        plugins=[plugin],
    ):
        out = await env.client.execute_workflow(
            ToolMiddlewareResolutionWorkflow.run,
            id=f"da-summarizer-tool-resolve-{uuid.uuid4()}",
            task_queue="da-summarizer-tool-resolve",
        )
    assert out == TemporalModel.__name__, out
