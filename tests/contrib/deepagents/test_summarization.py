"""String summarizer models must resolve to a durable ``TemporalModel``.

``SummarizationMiddleware`` resolves a name string via LangChain's
``init_chat_model`` binding, and ``create_summarization_tool_middleware`` via
a call-time import of ``deepagents._models.resolve_model`` — neither reads
the patched ``deepagents.graph`` seam, so in-workflow both built a real
provider client (the default stack is unaffected: it receives the
already-resolved agent model). One pin per seam.
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

# Bind deepagents symbols off importorskip modules: static imports cannot
# resolve on Python 3.10 environments, where deepagents is absent.
_backends_mod = pytest.importorskip("deepagents.backends")
_middleware_mod = pytest.importorskip("deepagents.middleware")
_summarization_mod = pytest.importorskip("deepagents.middleware.summarization")
StateBackend = _backends_mod.StateBackend
SummarizationMiddleware = _middleware_mod.SummarizationMiddleware
create_summarization_tool_middleware = (
    _summarization_mod.create_summarization_tool_middleware
)


@workflow.defn
class SummarizerResolutionWorkflow:
    @workflow.run
    async def run(self) -> str:
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
