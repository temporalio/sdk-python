import dataclasses
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

import pytest

import temporalio
import temporalio.converter
from temporalio import activity, workflow
from temporalio.api.common.v1 import Payload
from temporalio.client import Client, WorkflowFailureError, WorkflowHandle
from temporalio.common import RetryPolicy
from temporalio.converter import (
    ExternalStorage,
    StorageDriverClaim,
    StorageDriverRetrieveContext,
    StorageDriverStoreContext,
    StorageWarning,
)
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.testing._workflow import WorkflowEnvironment
from temporalio.worker import Replayer
from tests.helpers import assert_task_fail_eventually, new_worker
from tests.test_extstore import InMemoryTestDriver


@dataclass(frozen=True)
class ExtStoreActivityInput:
    input_data: str
    output_size: int
    pass


# ---------------------------------------------------------------------------
# Chained-activity scenario
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessDataInput:
    """Input for the first activity: generate a large result."""

    size: int


@dataclass(frozen=True)
class SummarizeInput:
    """Input for the second activity: receives the large result from the first."""

    data: str


@activity.defn
async def process_data(input: ProcessDataInput) -> str:
    """Produces a large string result that will be stored externally."""
    return "x" * input.size


@activity.defn
async def summarize(input: SummarizeInput) -> str:
    """Receives the large result and returns a short summary."""
    return f"received {len(input.data)} bytes"


@workflow.defn
class ChainedExtStoreWorkflow:
    """Workflow that passes a large activity result directly into a second activity.

    This mirrors a common customer pattern: activity A produces a large payload
    (e.g. a fetched document or ML inference result) which is too big to store
    inline in workflow history, and is then consumed by activity B.  External
    storage should transparently offload the payload between the two steps
    without any special handling in the workflow code.
    """

    @workflow.run
    async def run(self, payload_size: int) -> str:
        large_result = await workflow.execute_activity(
            process_data,
            ProcessDataInput(size=payload_size),
            schedule_to_close_timeout=timedelta(seconds=10),
        )
        return await workflow.execute_activity(
            summarize,
            SummarizeInput(data=large_result),
            schedule_to_close_timeout=timedelta(seconds=10),
        )


@activity.defn
async def ext_store_activity(
    input: ExtStoreActivityInput,
) -> str:
    return "ao" * int(input.output_size / 2)


@dataclass(frozen=True)
class ExtStoreWorkflowInput:
    input_data: str
    activity_input_size: int
    activity_output_size: int
    output_size: int
    max_activity_attempts: int | None = None


@workflow.defn
class ExtStoreWorkflow:
    @workflow.run
    async def run(self, input: ExtStoreWorkflowInput) -> str:
        retry_policy = (
            RetryPolicy(maximum_attempts=input.max_activity_attempts)
            if input.max_activity_attempts is not None
            else None
        )
        await workflow.execute_activity(
            ext_store_activity,
            ExtStoreActivityInput(
                input_data="ai" * int(input.activity_input_size / 2),
                output_size=input.activity_output_size,
            ),
            schedule_to_close_timeout=timedelta(seconds=3),
            retry_policy=retry_policy,
        )
        return "wo" * int(input.output_size / 2)


class BadTestDriver(InMemoryTestDriver):
    def __init__(
        self,
        driver_name: str = "bad-driver",
        no_store: bool = False,
        no_retrieve: bool = False,
        raise_payload_not_found: bool = False,
    ):
        super().__init__(driver_name)
        self._no_store = no_store
        self._no_retrieve = no_retrieve
        self._raise_payload_not_found = raise_payload_not_found

    async def store(
        self,
        context: StorageDriverStoreContext,
        payloads: Sequence[Payload],
    ) -> list[StorageDriverClaim]:
        if self._no_store:
            return []
        return await super().store(context, payloads)

    async def retrieve(
        self,
        context: StorageDriverRetrieveContext,
        claims: Sequence[StorageDriverClaim],
    ) -> list[Payload]:
        if self._no_retrieve:
            return []
        if self._raise_payload_not_found:
            raise ApplicationError(
                "Payload not found because the bucket does not exist.",
                type="BucketNotFoundError",
                non_retryable=True,
            )
        return await super().retrieve(context, claims)


async def test_extstore_activity_input_no_retrieve(
    env: WorkflowEnvironment,
):
    """When the driver's retrieve returns no payloads for an externalized
    activity input, the activity fails and the workflow terminates with a
    WorkflowFailureError wrapping an ActivityError."""
    driver = BadTestDriver(no_retrieve=True)

    client = await Client.connect(
        env.client.service_client.config.target_host,
        namespace=env.client.namespace,
        data_converter=dataclasses.replace(
            temporalio.converter.default(),
            external_storage=ExternalStorage(
                drivers=[driver],
                payload_size_threshold=1024,
            ),
        ),
    )

    async with new_worker(
        client, ExtStoreWorkflow, activities=[ext_store_activity]
    ) as worker:
        handle = await client.start_workflow(
            ExtStoreWorkflow.run,
            ExtStoreWorkflowInput(
                input_data="workflow input",
                activity_input_size=1000,
                activity_output_size=10,
                output_size=10,
                max_activity_attempts=1,
            ),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        with pytest.raises(WorkflowFailureError) as err:
            await handle.result()

        assert isinstance(err.value.cause, ActivityError)
        assert isinstance(err.value.cause.cause, ApplicationError)
        assert err.value.cause.cause.message == "Failed decoding arguments"


async def test_extstore_activity_result_no_store(
    env: WorkflowEnvironment,
):
    """When the driver's store returns no claims for an activity result that
    exceeds the size threshold, the activity fails to complete and the workflow
    terminates with a WorkflowFailureError wrapping an ActivityError."""
    driver = BadTestDriver(no_store=True)

    client = await Client.connect(
        env.client.service_client.config.target_host,
        namespace=env.client.namespace,
        data_converter=dataclasses.replace(
            temporalio.converter.default(),
            external_storage=ExternalStorage(
                drivers=[driver],
                payload_size_threshold=1024,
            ),
        ),
    )

    async with new_worker(
        client, ExtStoreWorkflow, activities=[ext_store_activity]
    ) as worker:
        handle = await client.start_workflow(
            ExtStoreWorkflow.run,
            ExtStoreWorkflowInput(
                input_data="workflow input",
                activity_input_size=10,
                activity_output_size=1000,
                output_size=10,
                max_activity_attempts=1,
            ),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        with pytest.raises(WorkflowFailureError) as err:
            await handle.result()

        assert isinstance(err.value.cause, ActivityError)
        assert isinstance(err.value.cause.cause, ApplicationError)
        assert (
            err.value.cause.cause.message
            == "Driver 'bad-driver' returned 0 claims, expected 1"
        )
        assert err.value.cause.cause.type == "ValueError"


async def test_extstore_worker_missing_driver(
    env: WorkflowEnvironment,
):
    """Validate that when a worker is provided a workflow history with
    external storage references and the worker is not configured for external
    storage, it will cause a workflow task failure.
    """
    driver = InMemoryTestDriver()

    far_client = await Client.connect(
        env.client.service_client.config.target_host,
        namespace=env.client.namespace,
        data_converter=dataclasses.replace(
            temporalio.converter.default(),
            external_storage=ExternalStorage(
                drivers=[driver],
                payload_size_threshold=1024,
            ),
        ),
    )

    worker_client = await Client.connect(
        env.client.service_client.config.target_host,
        namespace=env.client.namespace,
    )

    async with new_worker(
        worker_client, ExtStoreWorkflow, activities=[ext_store_activity]
    ) as worker:
        handle = await far_client.start_workflow(
            ExtStoreWorkflow.run,
            ExtStoreWorkflowInput(
                input_data="wi" * 1024,
                activity_input_size=10,
                activity_output_size=10,
                output_size=10,
            ),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        await assert_task_fail_eventually(handle)


async def test_extstore_payload_not_found_fails_workflow(
    env: WorkflowEnvironment,
):
    """When a non-retryable ApplicationError is raised while retrieving workflow input,
    the workflow must fail terminally (not retry as a task failure).
    """
    client = await Client.connect(
        env.client.service_client.config.target_host,
        namespace=env.client.namespace,
        data_converter=dataclasses.replace(
            temporalio.converter.default(),
            external_storage=ExternalStorage(
                drivers=[BadTestDriver(raise_payload_not_found=True)],
                payload_size_threshold=1024,
            ),
        ),
    )

    async with new_worker(
        client, ExtStoreWorkflow, activities=[ext_store_activity]
    ) as worker:
        handle = await client.start_workflow(
            ExtStoreWorkflow.run,
            ExtStoreWorkflowInput(
                input_data="wi" * 512,  # exceeds 1024-byte threshold
                activity_input_size=10,
                activity_output_size=10,
                output_size=10,
            ),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=10),
        )

        with pytest.raises(WorkflowFailureError) as exc_info:
            await handle.result()

        assert isinstance(exc_info.value.cause, ApplicationError)
        assert (
            exc_info.value.cause.message
            == "Payload not found because the bucket does not exist."
        )
        assert exc_info.value.cause.type == "BucketNotFoundError"
        assert exc_info.value.cause.non_retryable is True


async def _run_extstore_workflow_and_fetch_history(
    env: WorkflowEnvironment,
    driver: InMemoryTestDriver,
    *,
    input_data: str,
    activity_output_size: int = 10,
) -> WorkflowHandle:
    """Helper: run ExtStoreWorkflow with the given driver and return its history handle."""
    extstore_client = await Client.connect(
        env.client.service_client.config.target_host,
        namespace=env.client.namespace,
        data_converter=dataclasses.replace(
            temporalio.converter.default(),
            external_storage=ExternalStorage(
                drivers=[driver],
                payload_size_threshold=512,
            ),
        ),
    )
    async with new_worker(
        extstore_client, ExtStoreWorkflow, activities=[ext_store_activity]
    ) as worker:
        handle = await extstore_client.start_workflow(
            ExtStoreWorkflow.run,
            ExtStoreWorkflowInput(
                input_data=input_data,
                activity_input_size=10,
                activity_output_size=activity_output_size,
                output_size=10,
            ),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()
    return handle


async def test_replay_extstore_history_fails_without_extstore(
    env: WorkflowEnvironment,
) -> None:
    """A history with externalized workflow input fails to replay when the
    Replayer has no external storage configured."""
    driver = InMemoryTestDriver()
    handle = await _run_extstore_workflow_and_fetch_history(
        env,
        driver,
        input_data="wi" * 512,  # exceeds 512-byte threshold
    )
    history = await handle.fetch_history()

    # Replay without external storage — the reference payload cannot be decoded.
    # The middleware emits a StorageWarning when it encounters a reference payload
    # with no driver configured.
    with pytest.warns(
        StorageWarning,
        match=r"^\[TMPRL1105\] Detected externally stored payload\(s\) but external storage is not configured\.$",
    ):
        result = await Replayer(workflows=[ExtStoreWorkflow]).replay_workflow(
            history, raise_on_replay_failure=False
        )
    # Must be a task-failure RuntimeError, not a NondeterminismError — external
    # storage decode failures are distinct from workflow code changes.
    assert isinstance(result.replay_failure, RuntimeError)
    assert not isinstance(result.replay_failure, workflow.NondeterminismError)
    # The message is the full activation-completion failure string; the
    # "Failed decoding arguments" text from _convert_payloads is embedded in it.
    assert "Failed decoding arguments" in result.replay_failure.args[0]


async def test_replay_extstore_history_succeeds_with_correct_extstore(
    env: WorkflowEnvironment,
) -> None:
    """A history with externalized workflow input replays successfully when the
    Replayer is configured with the same storage driver that holds the data."""
    driver = InMemoryTestDriver()
    handle = await _run_extstore_workflow_and_fetch_history(
        env, driver, input_data="wi" * 512
    )
    history = await handle.fetch_history()

    # Replay with the same populated driver — must succeed.
    await Replayer(
        workflows=[ExtStoreWorkflow],
        data_converter=dataclasses.replace(
            temporalio.converter.default(),
            external_storage=ExternalStorage(
                drivers=[driver],
                payload_size_threshold=512,
            ),
        ),
    ).replay_workflow(history)


async def test_replay_extstore_history_fails_with_empty_driver(
    env: WorkflowEnvironment,
) -> None:
    """A history with external storage references fails to replay when the
    Replayer has external storage configured but the driver holds no data
    (simulates pointing at the wrong backend or a purged store)."""
    driver = InMemoryTestDriver()
    handle = await _run_extstore_workflow_and_fetch_history(
        env, driver, input_data="wi" * 512
    )
    history = await handle.fetch_history()

    # Replay with a fresh empty driver — retrieval will fail.
    result = await Replayer(
        workflows=[ExtStoreWorkflow],
        data_converter=dataclasses.replace(
            temporalio.converter.default(),
            external_storage=ExternalStorage(
                drivers=[InMemoryTestDriver()],
                payload_size_threshold=512,
            ),
        ),
    ).replay_workflow(history, raise_on_replay_failure=False)
    # InMemoryTestDriver raises ApplicationError for absent keys.
    # ApplicationError is re-raised without wrapping, so it propagates
    # through decode_activation (before the workflow task runs).  The core SDK
    # receives an activation failure, issues a FailWorkflow command, but the
    # next history event is ActivityTaskScheduled — causing a NondeterminismError.
    assert isinstance(result.replay_failure, workflow.NondeterminismError)


async def test_replay_extstore_activity_result_fails_without_extstore(
    env: WorkflowEnvironment,
) -> None:
    """A history where only the activity result was stored externally (the
    workflow input is small enough to be inline) also fails to replay without
    external storage — verifying that mid-workflow decode failures are caught."""
    driver = InMemoryTestDriver()
    handle = await _run_extstore_workflow_and_fetch_history(
        env,
        driver,
        input_data="small",  # well under 512 bytes — stays inline
        activity_output_size=2048,  # 2 KB result — stored externally
    )
    history = await handle.fetch_history()

    # Replay without external storage.  The workflow input decodes fine, but
    # when the ActivityTaskCompleted result is delivered back to the workflow
    # coroutine it cannot be decoded.
    with pytest.warns(
        StorageWarning,
        match=r"^\[TMPRL1105\] Detected externally stored payload\(s\) but external storage is not configured\.$",
    ):
        result = await Replayer(workflows=[ExtStoreWorkflow]).replay_workflow(
            history, raise_on_replay_failure=False
        )
    # Mid-workflow decode failure is still a task failure (RuntimeError), not
    # nondeterminism.
    assert isinstance(result.replay_failure, RuntimeError)
    assert not isinstance(result.replay_failure, workflow.NondeterminismError)
    # The message is the full activation-completion failure string; the
    # "Failed decoding arguments" text from _convert_payloads is embedded in it.
    assert "Failed decoding arguments" in result.replay_failure.args[0]


async def test_extstore_chained_activities(
    env: WorkflowEnvironment,
) -> None:
    """Large activity output is transparently offloaded and passed to a second activity.

    This is a representative customer scenario: activity A returns a payload that
    exceeds the size threshold (e.g. a fetched document), external storage offloads
    it so it never bloats workflow history, and activity B receives it as its input
    without any special handling in the workflow code.
    """
    driver = InMemoryTestDriver()

    client = await Client.connect(
        env.client.service_client.config.target_host,
        namespace=env.client.namespace,
        data_converter=dataclasses.replace(
            temporalio.converter.default(),
            external_storage=ExternalStorage(
                drivers=[driver],
                payload_size_threshold=1024,  # 1 KB threshold
            ),
        ),
    )

    # process_data returns 10 KB — well above the 1 KB threshold.
    payload_size = 10_000

    async with new_worker(
        client,
        ChainedExtStoreWorkflow,
        activities=[process_data, summarize],
    ) as worker:
        result = await client.execute_workflow(
            ChainedExtStoreWorkflow.run,
            payload_size,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )

    # The second activity received the full payload and summarized it correctly.
    assert result == f"received {payload_size} bytes"

    # External storage was actually used: the large activity result and its
    # re-use as the second activity's input should have triggered at least two
    # round-trips (one store on completion, one retrieve on the next WFT).
    assert driver._store_calls == 2
    assert driver._retrieve_calls == 2
