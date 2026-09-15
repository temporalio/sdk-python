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
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta

import pytest
from google.adk.platform import _random as adk_random
from google.adk.platform import time as adk_time
from google.adk.platform import uuid as adk_uuid

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.google_adk_agents import GoogleAdkPlugin, _plugin
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
    random_is_private_cached_stream: bool
    workflow_stream_unperturbed: bool


# Appended to by PlatformProviderWorkflow when it runs on an unsandboxed
# runner, which shares this module with the test (the sandbox imports its own
# copy). Lets a Replayer run hand its readings back to the test.
unsandboxed_readings: list[PlatformProviderReadings] = []


def reset_adk_providers_to_shipped_state() -> None:
    """Undo any earlier plugin install so a test proves its own install.

    Rebuilds each ADK seam as it ships: the standard-library ``_default_*``
    provider and a fresh ContextVar defaulting to it. The plugin rebinds
    both, so both must be restored.
    """
    adk_time._default_time_provider = time.time
    adk_time._time_provider_context_var = contextvars.ContextVar(
        "time_provider", default=adk_time._default_time_provider
    )
    adk_uuid._default_id_provider = lambda: str(uuid.uuid4())
    adk_uuid._id_provider_context_var = contextvars.ContextVar(
        "id_provider", default=adk_uuid._default_id_provider
    )
    adk_random._default_random_provider = lambda: adk_random._default_random
    adk_random._random_provider_context_var = contextvars.ContextVar(
        "random_provider", default=adk_random._default_random_provider
    )


@workflow.defn
class PlatformProviderWorkflow:
    @workflow.run
    async def run(self) -> PlatformProviderReadings:
        # ADK ids and randoms come from a private stream created via
        # workflow.new_random() on first use, so a mirror stream made the
        # same way reproduces the id from the same 128 bits.
        adk_id = adk_uuid.new_uuid()
        mirror = workflow.new_random()
        expected_id = str(uuid.UUID(int=mirror.getrandbits(128), version=4))
        adk_rng = adk_random.get_random()
        # The private stream and workflow.random() start from the same seed,
        # so if ADK's id draw had gone through workflow.random(), the user
        # stream's next value would no longer match a fresh same-seed stream.
        probe = workflow.new_random()
        readings = PlatformProviderReadings(
            adk_time=adk_time.get_time(),
            workflow_time=workflow.time(),
            adk_id=adk_id,
            expected_id=expected_id,
            random_is_private_cached_stream=(
                adk_rng is not workflow.random() and adk_random.get_random() is adk_rng
            ),
            workflow_stream_unperturbed=workflow.random().random() == probe.random(),
        )
        unsandboxed_readings.append(readings)
        return readings

    @workflow.query
    def query_adk_id(self) -> str:
        # Read-only contexts get nondeterministic entropy; the cached private
        # stream must stay untouched (QueryDuringRunWorkflow proves that).
        return adk_uuid.new_uuid()


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
        # Read-only fallback: a query still gets a valid (nondeterministic)
        # uuid rather than an error.
        assert uuid.UUID(await handle.query(PlatformProviderWorkflow.query_adk_id))
        history = await handle.fetch_history()

    assert readings.adk_time == readings.workflow_time
    assert readings.adk_id == readings.expected_id
    assert readings.random_is_private_cached_stream
    assert readings.workflow_stream_unperturbed

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


@dataclass
class SetResetReadings:
    overridden_time: float
    time_after_reset: float
    workflow_time: float
    overridden_id: str
    id_after_reset: str
    overridden_random_was_adk_default: bool
    random_after_reset_is_private_stream: bool


# Same hand-back mechanism as unsandboxed_readings above.
unsandboxed_set_reset_readings: list[SetResetReadings] = []


@workflow.defn
class SetResetProviderWorkflow:
    """Exercises ADK's public set-then-reset cycle inside a workflow.

    reset_*_provider() restores the module's _default_* binding, so the
    plugin must have rebound that too: otherwise a reset lands on the
    standard-library provider and the rest of the run is nondeterministic.
    """

    @workflow.run
    async def run(self) -> SetResetReadings:
        private_rng = adk_random.get_random()

        adk_time.set_time_provider(lambda: -1.0)
        overridden_time = adk_time.get_time()
        adk_time.reset_time_provider()

        adk_uuid.set_id_provider(lambda: "fixed-id")
        overridden_id = adk_uuid.new_uuid()
        adk_uuid.reset_id_provider()

        # ADK's shipped default instance still exists on the module; use it
        # as the override to avoid constructing randomness in workflow code.
        adk_random.set_random_provider(lambda: adk_random._default_random)
        overridden_random_was_adk_default = (
            adk_random.get_random() is adk_random._default_random
        )
        adk_random.reset_random_provider()
        after_reset_rng = adk_random.get_random()

        readings = SetResetReadings(
            overridden_time=overridden_time,
            time_after_reset=adk_time.get_time(),
            workflow_time=workflow.time(),
            overridden_id=overridden_id,
            id_after_reset=adk_uuid.new_uuid(),
            overridden_random_was_adk_default=overridden_random_was_adk_default,
            random_after_reset_is_private_stream=(
                after_reset_rng is private_rng
                and after_reset_rng is not adk_random._default_random
            ),
        )
        unsandboxed_set_reset_readings.append(readings)
        return readings


@pytest.mark.parametrize(
    "workflow_runner",
    [SandboxedWorkflowRunner(), UnsandboxedWorkflowRunner()],
    ids=["sandboxed", "unsandboxed"],
)
async def test_reset_in_workflow_restores_deterministic_providers(
    client: Client, workflow_runner: WorkflowRunner
) -> None:
    reset_adk_providers_to_shipped_state()
    new_config = client.config()
    new_config["plugins"] = [GoogleAdkPlugin()]
    client = Client(**new_config)

    task_queue = f"adk-set-reset-{uuid.uuid4()}"
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[SetResetProviderWorkflow],
        workflow_runner=workflow_runner,
    ):
        handle = await client.start_workflow(
            SetResetProviderWorkflow.run,
            id=f"adk-set-reset-{uuid.uuid4()}",
            task_queue=task_queue,
            execution_timeout=timedelta(seconds=60),
        )
        readings = await handle.result()
        history = await handle.fetch_history()

    assert readings.overridden_time == -1.0
    assert readings.time_after_reset == readings.workflow_time
    assert readings.overridden_id == "fixed-id"
    assert uuid.UUID(readings.id_after_reset).version == 4
    assert readings.overridden_random_was_adk_default
    assert readings.random_after_reset_is_private_stream

    # If reset had restored wall-clock/stdlib providers, the post-reset
    # readings could not reproduce from history.
    reset_adk_providers_to_shipped_state()
    unsandboxed_set_reset_readings.clear()
    await Replayer(
        workflows=[SetResetProviderWorkflow],
        plugins=[GoogleAdkPlugin()],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ).replay_workflow(history)
    assert unsandboxed_set_reset_readings == [readings]


# Same hand-back mechanism as unsandboxed_readings above.
unsandboxed_query_run_ids: list[list[str]] = []


@workflow.defn
class QueryDuringRunWorkflow:
    """Proves query-handler draws never advance the private ADK stream.

    The run draws one id, waits for a signal (queries happen here), then
    draws another. Queries do not run during replay, so if a query had
    advanced the cached stream, the second id could not reproduce on replay.
    """

    def __init__(self) -> None:
        self.proceed = False

    @workflow.run
    async def run(self) -> list[str]:
        ids = [adk_uuid.new_uuid()]
        await workflow.wait_condition(lambda: self.proceed)
        ids.append(adk_uuid.new_uuid())
        unsandboxed_query_run_ids.append(ids)
        return ids

    @workflow.signal
    def go(self) -> None:
        self.proceed = True

    @workflow.query
    def query_adk_id(self) -> str:
        return adk_uuid.new_uuid()


@pytest.mark.parametrize(
    "workflow_runner",
    [SandboxedWorkflowRunner(), UnsandboxedWorkflowRunner()],
    ids=["sandboxed", "unsandboxed"],
)
async def test_query_draws_do_not_advance_private_stream(
    client: Client, workflow_runner: WorkflowRunner
) -> None:
    reset_adk_providers_to_shipped_state()
    new_config = client.config()
    new_config["plugins"] = [GoogleAdkPlugin()]
    client = Client(**new_config)

    task_queue = f"adk-query-stream-{uuid.uuid4()}"
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[QueryDuringRunWorkflow],
        workflow_runner=workflow_runner,
    ):
        handle = await client.start_workflow(
            QueryDuringRunWorkflow.run,
            id=f"adk-query-stream-{uuid.uuid4()}",
            task_queue=task_queue,
            execution_timeout=timedelta(seconds=60),
        )
        # Draw through the read-only fallback between the run's two draws.
        for _ in range(3):
            assert uuid.UUID(await handle.query(QueryDuringRunWorkflow.query_adk_id))
        await handle.signal(QueryDuringRunWorkflow.go)
        ids = await handle.result()
        history = await handle.fetch_history()

    assert len(ids) == 2 and ids[0] != ids[1]

    # Replay never runs the queries; the ids only reproduce if the query
    # draws left the private stream untouched.
    reset_adk_providers_to_shipped_state()
    unsandboxed_query_run_ids.clear()
    await Replayer(
        workflows=[QueryDuringRunWorkflow],
        plugins=[GoogleAdkPlugin()],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ).replay_workflow(history)
    assert unsandboxed_query_run_ids == [ids]


def test_reset_outside_workflow_restores_installed_provider() -> None:
    reset_adk_providers_to_shipped_state()
    _plugin.setup_deterministic_runtime()

    def set_reset_read() -> None:
        adk_time.set_time_provider(lambda: 1.0)
        assert adk_time.get_time() == 1.0
        adk_time.reset_time_provider()
        assert (
            adk_time._time_provider_context_var.get()
            is _plugin._deterministic_time_provider
        )
        adk_uuid.set_id_provider(lambda: "fixed-id")
        adk_uuid.reset_id_provider()
        assert (
            adk_uuid._id_provider_context_var.get()
            is _plugin._deterministic_id_provider
        )
        adk_random.set_random_provider(lambda: adk_random._default_random)
        adk_random.reset_random_provider()
        assert (
            adk_random._random_provider_context_var.get()
            is _plugin._deterministic_random_provider
        )

    # Run in a copied context so the overrides do not leak into other tests.
    contextvars.copy_context().run(set_reset_read)


def test_providers_are_defaults_visible_from_new_threads() -> None:
    reset_adk_providers_to_shipped_state()
    _plugin.setup_deterministic_runtime()

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

    assert time_provider is _plugin._deterministic_time_provider
    assert id_provider is _plugin._deterministic_id_provider
    assert random_provider is _plugin._deterministic_random_provider


def test_providers_fall_back_outside_workflow() -> None:
    _plugin.setup_deterministic_runtime()

    assert (
        adk_time._time_provider_context_var.get()
        is _plugin._deterministic_time_provider
    )
    assert adk_time.get_time() == pytest.approx(time.time(), abs=5)
    assert adk_uuid._id_provider_context_var.get() is _plugin._deterministic_id_provider
    assert uuid.UUID(adk_uuid.new_uuid()).version == 4
    assert (
        adk_random._random_provider_context_var.get()
        is _plugin._deterministic_random_provider
    )
    # One shared instance, so RNG state carries across calls as ADK expects.
    rng = adk_random.get_random()
    assert isinstance(rng, random.Random)
    assert rng is _plugin._random_outside_workflow
    assert adk_random.get_random() is rng


def test_setup_deterministic_runtime_is_idempotent() -> None:
    _plugin.setup_deterministic_runtime()
    time_var = adk_time._time_provider_context_var
    id_var = adk_uuid._id_provider_context_var
    random_var = adk_random._random_provider_context_var

    _plugin.setup_deterministic_runtime()

    assert adk_time._time_provider_context_var is time_var
    assert adk_uuid._id_provider_context_var is id_var
    assert adk_random._random_provider_context_var is random_var


def test_install_warns_when_replacing_provider_set_before_install() -> None:
    reset_adk_providers_to_shipped_state()

    def set_then_install() -> None:
        adk_time.set_time_provider(lambda: 1.0)
        with pytest.warns(UserWarning, match="set in this context before"):
            _plugin.setup_deterministic_runtime()
        # The earlier override lives on the replaced variable and is ignored.
        assert adk_time.get_time() != 1.0

    # Run in a copied context so the override does not leak into other tests.
    contextvars.copy_context().run(set_then_install)

    # Installing over untouched seams is silent.
    reset_adk_providers_to_shipped_state()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _plugin.setup_deterministic_runtime()


def test_adk_setters_still_override_in_calling_context() -> None:
    _plugin.setup_deterministic_runtime()

    def override_and_read() -> float:
        adk_time.set_time_provider(lambda: 1.0)
        return adk_time.get_time()

    # Run in a copied context so the override does not leak into other tests.
    assert contextvars.copy_context().run(override_and_read) == 1.0
    assert adk_time.get_time() != 1.0
