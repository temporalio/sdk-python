"""Replay safety tests for temporalio.contrib.tenuo.

Proves that replaying the recorded history of a completed warranted workflow
does NOT produce non-determinism errors or repeated side effects.

Each test follows the same pattern the Temporal review team expects:

1. Run an authorized workflow to completion (warrant + PoP signing).
2. Fetch the workflow history via ``handle.fetch_history()``.
3. Create a **fresh** ``TenuoPlugin`` instance (simulating a restarted worker).
4. Replay the history with ``Replayer(workflows=[...], plugins=[replay_plugin])``.
5. Assert ``replay_failure is None`` — no non-determinism errors.

This validates that:
- PoP signatures generated via ``tenuo_execute_activity`` (which uses
  ``workflow.now()`` for timestamps) are deterministic across execution and
  replay.
- Multiple sequential PoP-signed activity calls replay in the same order.
- No side effects (network calls, randomness, wall-clock reads) are repeated.
"""

from datetime import timedelta

import pytest

import tenuo
from tenuo.temporal import (
    KeyResolver,
    TenuoPluginConfig,
    execute_workflow_authorized,
    tenuo_execute_activity,
)

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.tenuo import TenuoPlugin
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker


KEY_ID = "replay-key"


class DictKeyResolver(KeyResolver):
    """Pre-loaded key resolver for tests (no os.environ access)."""

    def __init__(self, keys: dict) -> None:
        """Initialize with a dict of key_id -> SigningKey."""
        self.keys = keys

    async def resolve(self, key_id: str) -> tenuo.SigningKey:
        """Resolve a key by ID."""
        return self.resolve_sync(key_id)

    def resolve_sync(self, key_id: str) -> tenuo.SigningKey:
        """Resolve a key by ID synchronously."""
        if key_id not in self.keys:
            raise ValueError(f"Key {key_id!r} not found")
        return self.keys[key_id]


# ---------------------------------------------------------------------------
# Activities — realistic agentic tools
# ---------------------------------------------------------------------------


@activity.defn
async def lookup_customer(customer_id: str) -> dict:
    """Fetch customer record from the CRM."""
    return {"id": customer_id, "name": "Alice", "plan": "pro"}


@activity.defn
async def call_llm(prompt: str, model: str, max_tokens: int) -> str:
    """Generate a response from an LLM."""
    return f"LLM response (model={model})"


# ---------------------------------------------------------------------------
# Workflows — exercise tenuo_execute_activity (PoP signing on hot path)
# ---------------------------------------------------------------------------


@workflow.defn
class SingleToolWorkflow:
    """Single authorized activity call with PoP signing."""

    @workflow.run
    async def run(self, customer_id: str) -> str:
        """Look up a customer — one PoP signature generated."""
        result = await tenuo_execute_activity(
            lookup_customer,
            args=[customer_id],
            start_to_close_timeout=timedelta(seconds=10),
        )
        return result["name"]


@workflow.defn
class MultiToolWorkflow:
    """Two sequential authorized activities — PoP order must be deterministic."""

    @workflow.run
    async def run(self, customer_id: str) -> str:
        """Look up customer then call LLM — two PoP signatures in order."""
        customer = await tenuo_execute_activity(
            lookup_customer,
            args=[customer_id],
            start_to_close_timeout=timedelta(seconds=10),
        )
        return await tenuo_execute_activity(
            call_llm,
            args=[f"Help {customer['name']}", "gpt-4o-mini", 256],
            start_to_close_timeout=timedelta(seconds=30),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(signing_key: tenuo.SigningKey) -> TenuoPluginConfig:
    return TenuoPluginConfig(
        key_resolver=DictKeyResolver({KEY_ID: signing_key}),
        trusted_roots=[signing_key.public_key],
    )


async def _record_and_replay(
    signing_key: tenuo.SigningKey,
    workflow_cls: type,
    workflow_input: str,
    workflow_id: str,
    task_queue: str,
    activities: list,
    tools: list[str],
) -> None:
    """Run a warranted workflow, capture history, replay with a fresh plugin.

    Steps:
      1. Start a local Temporal server.
      2. Run the workflow to completion via execute_workflow_authorized().
      3. Fetch the completed workflow's history.
      4. Create a brand-new TenuoPlugin (fresh interceptor state).
      5. Replay the history through Replayer and assert no failure.
    """
    # -- Step 1-3: record -------------------------------------------------------
    plugin = TenuoPlugin(_make_config(signing_key))

    warrant = (
        tenuo.Warrant.mint_builder()
        .tools(tools)
        .holder(signing_key.public_key)
        .ttl(3600)
        .mint(signing_key)
    )

    async with await WorkflowEnvironment.start_local() as env:
        client = Client(
            env.client.service_client,
            namespace=env.client.namespace,
            data_converter=env.client.data_converter,
            plugins=[plugin],
        )

        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[workflow_cls],
            activities=activities,
        ):
            result = await execute_workflow_authorized(
                client=client,
                workflow_run_fn=workflow_cls.run,
                args=[workflow_input],
                warrant=warrant,
                key_id=KEY_ID,
                workflow_id=workflow_id,
                task_queue=task_queue,
            )
            assert isinstance(result, str), f"Expected str result, got {type(result)}"

            handle = client.get_workflow_handle(workflow_id)
            history = await handle.fetch_history()

    # -- Step 4-5: replay with fresh plugin ------------------------------------
    replay_plugin = TenuoPlugin(_make_config(signing_key))

    replay_result = await Replayer(
        workflows=[workflow_cls],
        plugins=[replay_plugin],
    ).replay_workflow(history, raise_on_replay_failure=False)

    assert replay_result.replay_failure is None, (
        f"Replay produced a non-determinism error, meaning the TenuoPlugin "
        f"interceptor generated different commands on replay than during the "
        f"original execution:\n{replay_result.replay_failure}"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.timeout(120)
async def test_single_tool_replay() -> None:
    """Record a customer lookup workflow, replay it — no non-determinism.

    Exercises the full PoP signing path: tenuo_execute_activity generates a PoP
    header using workflow.now() as the timestamp. On replay, Temporal feeds the
    same recorded timestamp back, so the interceptor must produce identical
    commands.
    """
    await _record_and_replay(
        signing_key=tenuo.SigningKey.generate(),
        workflow_cls=SingleToolWorkflow,
        workflow_input="cust-1001",
        workflow_id="replay-single-1",
        task_queue="replay-single-queue",
        activities=[lookup_customer, call_llm],
        tools=["lookup_customer"],
    )


@pytest.mark.timeout(120)
async def test_multi_tool_replay_ordering() -> None:
    """Record a lookup + LLM workflow, replay it — PoP order is deterministic.

    Two sequential tenuo_execute_activity calls each generate a separate PoP
    signature with a (potentially different) workflow.now() timestamp. Replay
    must produce commands in the same order with the same PoP payloads.
    """
    await _record_and_replay(
        signing_key=tenuo.SigningKey.generate(),
        workflow_cls=MultiToolWorkflow,
        workflow_input="cust-2002",
        workflow_id="replay-multi-1",
        task_queue="replay-multi-queue",
        activities=[lookup_customer, call_llm],
        tools=["lookup_customer", "call_llm"],
    )
