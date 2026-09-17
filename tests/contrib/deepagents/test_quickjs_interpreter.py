"""The ``langchain-quickjs`` code interpreter runs in-workflow with durable bridges.

The middleware's ``eval`` tool runs model-written JavaScript in a QuickJS VM.
Upstream hosts the VM on its own thread and wakes the caller through
``call_soon_threadsafe``, which the workflow event loop cannot service: without
the plugin's inline patch the workflow parks forever after the first model
call. With it, JavaScript is workflow code, a sub-agent dispatched from
JavaScript via ``task()`` runs in-workflow (so its model call is an
``invoke_model`` activity), and a PTC tool wrapped with ``tool_as_activity``
crosses as an ``invoke_tool`` activity. Both scenarios replay from history.
"""

from __future__ import annotations

import sys
import uuid
from datetime import timedelta

import pytest

from temporalio.testing import WorkflowEnvironment

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="deepagents requires Python >= 3.11"
)
pytest.importorskip("deepagents")
pytest.importorskip("langchain_core")
pytest.importorskip("langchain_quickjs")

from temporalio import workflow  # noqa: E402
from temporalio.worker import Replayer, Worker  # noqa: E402
from tests.contrib.deepagents.helpers import count_scheduled_activities  # noqa: E402

# Bind deepagents symbols off the module importorskip returns: a static
# `from deepagents import ...` cannot resolve on Python 3.10 (deepagents
# needs >= 3.11), and with the package absent the type checkers mis-resolve
# the name against this same-named test directory.
create_deep_agent = pytest.importorskip("deepagents").create_deep_agent
CodeInterpreterMiddleware = pytest.importorskip(
    "langchain_quickjs"
).CodeInterpreterMiddleware

with workflow.unsafe.imports_passed_through():
    from langchain_core.messages import AIMessage

    from temporalio.contrib.deepagents import DeepAgentsPlugin, tool_as_activity
    from temporalio.contrib.deepagents.testing import MockTool, mock_model_provider

INVOKE_MODEL = "deepagents.invoke_model"
INVOKE_TOOL = "deepagents.invoke_tool"

# One eval: a PTC tool call, then a sub-agent dispatched from JavaScript.
_JS = (
    'const found = await tools.lookup({q: "x"}); '
    'const summary = await task({description: "Summarize", subagentType: "researcher"}); '
    "`${found}|${summary}`"
)


@workflow.defn
class InterpreterWorkflow:
    @workflow.run
    async def run(self, question: str) -> str:
        lookup = tool_as_activity(
            MockTool(name="lookup", description="Look up q.", result="looked-up"),
            start_to_close_timeout=timedelta(seconds=10),
        )
        agent = create_deep_agent(
            model="anthropic:claude-sonnet-4-5",
            system_prompt="You coordinate.",
            tools=[lookup],
            subagents=[
                {
                    "name": "researcher",
                    "description": "Researches.",
                    "system_prompt": "You research.",
                }
            ],
            middleware=[CodeInterpreterMiddleware(ptc=["lookup"])],
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": question}]}
        )
        # The eval tool's result carries what crossed back through JavaScript.
        return str(result["messages"][-2].content)


def _plugin() -> DeepAgentsPlugin:
    # Main agent asks for one eval; the sub-agent answers; the main agent finishes.
    return DeepAgentsPlugin(
        model_provider=mock_model_provider(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"name": "eval", "args": {"code": _JS}, "id": "c1"}],
                ),
                "Researcher findings.",
                "Final answer.",
            ]
        ),
    )


async def _run_and_replay(env: WorkflowEnvironment, **worker_kwargs: object) -> None:
    plugin = _plugin()
    task_queue = f"da-quickjs-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[InterpreterWorkflow],
        plugins=[plugin],
        **worker_kwargs,  # type: ignore[arg-type]
    ):
        handle = await env.client.start_workflow(
            InterpreterWorkflow.run,
            "Go",
            id=f"da-quickjs-{uuid.uuid4()}",
            task_queue=task_queue,
            execution_timeout=timedelta(seconds=60),
        )
        out = await handle.result()
        history = await handle.fetch_history()

    assert out == "<result>looked-up|Researcher findings.</result>"
    counts = await count_scheduled_activities(handle)
    # main -> eval, the sub-agent dispatched from JavaScript, main -> final.
    assert counts[INVOKE_MODEL] == 3, counts
    # The PTC call reached the tool_as_activity wrapper, not the tool body.
    assert counts[INVOKE_TOOL] == 1, counts

    # The JavaScript re-executes on replay against the recorded activity results.
    await Replayer(workflows=[InterpreterWorkflow], plugins=[plugin]).replay_workflow(
        history
    )


@pytest.mark.asyncio
async def test_interpreter_runs_in_workflow_with_durable_bridges(
    env: WorkflowEnvironment,
) -> None:
    await _run_and_replay(env)


@pytest.mark.asyncio
async def test_interpreter_survives_cache_eviction(env: WorkflowEnvironment) -> None:
    # Every activation replays from scratch, so the eval (which spans a tool
    # activity and a sub-agent's model activity) is re-driven several times.
    await _run_and_replay(env, max_cached_workflows=0)


def test_inline_patch_is_inert_outside_workflows() -> None:
    # Activities and clients in the same process keep upstream's thread-hosted
    # VM; only in-workflow calls run inline.
    from quickjs_rs.threading import ThreadWorker

    from temporalio.contrib.deepagents import _quickjs

    _quickjs.install_quickjs_inline_patch()
    try:

        async def probe() -> str:
            return "ran"

        worker = ThreadWorker(name="probe")
        try:
            assert worker.run_sync(probe()) == "ran"
            assert worker._thread is not None, "expected upstream's worker thread"
        finally:
            worker.close()
    finally:
        _quickjs.uninstall_quickjs_inline_patch()
