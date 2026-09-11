"""Tests that GoogleAdkPlugin's deterministic providers reach workflow code.

ADK reads its time, id, and random providers from contextvars.ContextVars.
Workflow tasks run on the worker's thread pool, whose threads start with an
empty context, so a provider merely set in the worker's context is invisible
there and ADK falls back to wall-clock time and random UUIDs. The plugin must
install the providers so that they are visible from every context.
"""

import contextvars
import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta

import pytest
from google.adk.platform import _random as adk_random
from google.adk.platform import time as adk_time
from google.adk.platform import uuid as adk_uuid

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.google_adk_agents import GoogleAdkPlugin
from temporalio.contrib.google_adk_agents._plugin import setup_deterministic_runtime
from temporalio.worker import (
    Replayer,
    UnsandboxedWorkflowRunner,
    Worker,
    WorkflowRunner,
)
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner


@dataclass
class PlatformProviderReadings:
    adk_time: float
    workflow_time: float
    adk_id: str
    expected_id: str
    random_is_workflow_random: bool


# Appended to by PlatformProviderWorkflow when it runs on an unsandboxed
# runner, which shares this module with the test (the sandbox imports its own
# copy). Lets a Replayer run hand its readings back to the test.
unsandboxed_readings: list[PlatformProviderReadings] = []


def reset_adk_providers_to_shipped_state() -> None:
    """Undo any earlier plugin install so a test proves its own install.

    Rebuilds each ADK seam as it ships: a fresh ContextVar defaulting to
    ADK's own provider.
    """
    adk_time._time_provider_context_var = contextvars.ContextVar(
        "time_provider", default=adk_time._default_time_provider
    )
    adk_uuid._id_provider_context_var = contextvars.ContextVar(
        "id_provider", default=adk_uuid._default_id_provider
    )
    adk_random._random_provider_context_var = contextvars.ContextVar(
        "random_provider", default=adk_random._default_random_provider
    )


@workflow.defn
class PlatformProviderWorkflow:
    @workflow.run
    async def run(self) -> PlatformProviderReadings:
        rng = workflow.random()
        # new_uuid() and workflow.uuid4() both consume the random stream, so
        # rewind it in between: from the same state they must agree.
        state = rng.getstate()
        adk_id = adk_uuid.new_uuid()
        rng.setstate(state)
        readings = PlatformProviderReadings(
            adk_time=adk_time.get_time(),
            workflow_time=workflow.now().timestamp(),
            adk_id=adk_id,
            expected_id=str(workflow.uuid4()),
            random_is_workflow_random=adk_random.get_random() is rng,
        )
        unsandboxed_readings.append(readings)
        return readings


@pytest.mark.parametrize(
    "workflow_runner",
    [SandboxedWorkflowRunner(), UnsandboxedWorkflowRunner()],
    ids=["sandboxed", "unsandboxed"],
)
async def test_providers_apply_inside_workflow_tasks(
    client: Client, workflow_runner: WorkflowRunner
) -> None:
    reset_adk_providers_to_shipped_state()
    new_config = client.config()
    new_config["plugins"] = [GoogleAdkPlugin()]
    client = Client(**new_config)

    task_queue = f"adk-platform-providers-{uuid.uuid4()}"
    # Not debug mode, so activations run on the workflow task executor's
    # threads as they do in production.
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[PlatformProviderWorkflow],
        workflow_runner=workflow_runner,
    ):
        handle = await client.start_workflow(
            PlatformProviderWorkflow.run,
            id=f"adk-platform-providers-{uuid.uuid4()}",
            task_queue=task_queue,
            execution_timeout=timedelta(seconds=60),
        )
        readings = await handle.result()
        history = await handle.fetch_history()

    assert readings.adk_time == readings.workflow_time
    assert readings.adk_id == readings.expected_id
    assert readings.random_is_workflow_random

    # The values derive from history, so a replay reproduces them exactly.
    # Replay unsandboxed so the workflow can hand its readings back.
    reset_adk_providers_to_shipped_state()
    unsandboxed_readings.clear()
    await Replayer(
        workflows=[PlatformProviderWorkflow],
        plugins=[GoogleAdkPlugin()],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ).replay_workflow(history)
    assert unsandboxed_readings == [readings]


def test_providers_are_defaults_visible_from_new_threads() -> None:
    reset_adk_providers_to_shipped_state()
    setup_deterministic_runtime()

    def read_providers() -> tuple[object, object, object]:
        # A new thread starts with an empty context; make that explicit so the
        # check does not depend on the interpreter's thread-inheritance flag.
        return contextvars.Context().run(
            lambda: (
                adk_time._time_provider_context_var.get(),
                adk_uuid._id_provider_context_var.get(),
                adk_random._random_provider_context_var.get(),
            )
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        time_provider, id_provider, random_provider = executor.submit(
            read_providers
        ).result()

    assert time_provider is not adk_time._default_time_provider
    assert id_provider is not adk_uuid._default_id_provider
    assert random_provider is not adk_random._default_random_provider


def test_providers_fall_back_outside_workflow() -> None:
    setup_deterministic_runtime()

    assert adk_time.get_time() == pytest.approx(time.time(), abs=5)
    assert uuid.UUID(adk_uuid.new_uuid()).version == 4
    rng = adk_random.get_random()
    assert isinstance(rng, random.Random)
    # One shared instance, so RNG state carries across calls as ADK expects.
    assert adk_random.get_random() is rng


def test_setup_deterministic_runtime_is_idempotent() -> None:
    setup_deterministic_runtime()
    time_var = adk_time._time_provider_context_var
    id_var = adk_uuid._id_provider_context_var
    random_var = adk_random._random_provider_context_var

    setup_deterministic_runtime()

    assert adk_time._time_provider_context_var is time_var
    assert adk_uuid._id_provider_context_var is id_var
    assert adk_random._random_provider_context_var is random_var


def test_adk_setters_still_override_in_calling_context() -> None:
    setup_deterministic_runtime()

    def override_and_read() -> float:
        adk_time.set_time_provider(lambda: 1.0)
        return adk_time.get_time()

    # Run in a copied context so the override does not leak into other tests.
    assert contextvars.copy_context().run(override_and_read) == 1.0
    assert adk_time.get_time() != 1.0
