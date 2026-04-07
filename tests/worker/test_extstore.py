import dataclasses
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from unittest import mock

import pytest

import temporalio
import temporalio.bridge.client
import temporalio.bridge.worker
import temporalio.client
import temporalio.converter
import temporalio.worker._workflow
from temporalio import activity, workflow
from temporalio.api.common.v1 import Payload
from temporalio.client import Client, WorkflowFailureError, WorkflowHandle
from temporalio.common import RetryPolicy
from temporalio.converter import (
    ExternalStorage,
    StorageDriver,
    StorageDriverActivityInfo,
    StorageDriverClaim,
    StorageDriverRetrieveContext,
    StorageDriverStoreContext,
    StorageDriverWorkflowInfo,
    StorageWarning,
)
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.testing._workflow import WorkflowEnvironment
from temporalio.worker import Replayer
from tests.helpers import LogCapturer, assert_task_fail_eventually, new_worker
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


async def test_worker_storage_drivers_populated_from_client(
    env: WorkflowEnvironment,
):
    """Worker._storage_drivers is populated from the client's ExternalStorage and
    passed to the bridge config as a set of driver type strings."""

    class DifferentTestDriver(InMemoryTestDriver):
        def __init__(self, driver_name: str):
            super().__init__(driver_name=driver_name)

    driver1 = InMemoryTestDriver(driver_name="driver1")
    driver2 = InMemoryTestDriver(driver_name="driver2")
    driver3 = DifferentTestDriver(driver_name="driver3")

    client = await Client.connect(
        env.client.service_client.config.target_host,
        namespace=env.client.namespace,
        data_converter=dataclasses.replace(
            temporalio.converter.default(),
            external_storage=ExternalStorage(
                drivers=[driver1, driver2, driver3],
                driver_selector=lambda _context, _payload: driver1,
                payload_size_threshold=0,
            ),
        ),
    )

    captured_config: list[temporalio.bridge.worker.WorkerConfig] = []
    original_create = temporalio.bridge.worker.Worker.create

    def capture_config(
        bridge_client: temporalio.bridge.client.Client,
        config: temporalio.bridge.worker.WorkerConfig,
    ):
        captured_config.append(config)
        return original_create(bridge_client, config)

    with mock.patch.object(
        temporalio.bridge.worker.Worker, "create", side_effect=capture_config
    ):
        async with new_worker(
            client, ExtStoreWorkflow, activities=[ext_store_activity]
        ) as worker:
            assert worker._storage_drivers == [driver1, driver2, driver3]

    assert len(captured_config) == 1
    assert captured_config[0].storage_drivers == {driver1.type(), driver3.type()}


async def test_worker_storage_drivers_empty_without_external_storage(
    env: WorkflowEnvironment,
):
    """Worker._storage_drivers is empty when the client has no ExternalStorage."""
    async with new_worker(
        env.client, ExtStoreWorkflow, activities=[ext_store_activity]
    ) as worker:
        assert worker._storage_drivers == []


# ---------------------------------------------------------------------------
# TMPRL1104 workflow task duration logging
# ---------------------------------------------------------------------------

_workflow_logger = logging.getLogger(temporalio.worker._workflow.__name__)


def _tmprl1104_records(capturer: LogCapturer) -> list[logging.LogRecord]:
    """Return all TMPRL1104 log records from the capturer."""
    return capturer.find_all(lambda r: r.getMessage().startswith("[TMPRL1104]"))


async def _expected_payload_size(
    converter: temporalio.converter.DataConverter, value: object
) -> int:
    """Encode a value and return the protobuf ByteSize of the resulting payload."""
    payloads = converter.payload_converter.to_payloads([value])
    return payloads[0].ByteSize()


@workflow.defn
class SimpleWorkflow:
    """Minimal workflow for testing logging without external storage."""

    @workflow.run
    async def run(self) -> str:
        return "done"


async def test_tmprl1104_no_extstore(env: WorkflowEnvironment) -> None:
    """Without external storage, TMPRL1104 logs contain duration but no
    download/upload metrics."""
    with LogCapturer().logs_captured(_workflow_logger, level=logging.DEBUG) as capturer:
        async with new_worker(env.client, SimpleWorkflow) as worker:
            await env.client.execute_workflow(
                SimpleWorkflow.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

    records = _tmprl1104_records(capturer)
    assert len(records) == 1
    record = records[0]
    assert record.getMessage().startswith(
        "[TMPRL1104] Workflow task duration information ("
    )
    assert hasattr(record, "workflow_task_duration")
    assert hasattr(record, "event_id")
    # No external storage — download/upload fields must be absent
    assert not hasattr(record, "payload_download_count")
    assert not hasattr(record, "payload_download_size")
    assert not hasattr(record, "payload_download_duration")
    assert not hasattr(record, "payload_upload_count")
    assert not hasattr(record, "payload_upload_size")
    assert not hasattr(record, "payload_upload_duration")


async def test_tmprl1104_with_extstore_download(env: WorkflowEnvironment) -> None:
    """When external storage decodes payloads, TMPRL1104 logs include download
    metrics on the activation that retrieves them."""
    driver = InMemoryTestDriver()
    data_converter = dataclasses.replace(
        temporalio.converter.default(),
        external_storage=ExternalStorage(
            drivers=[driver],
            payload_size_threshold=512,
        ),
    )
    client = await Client.connect(
        env.client.service_client.config.target_host,
        namespace=env.client.namespace,
        data_converter=data_converter,
    )

    wf_input = ExtStoreWorkflowInput(
        input_data="wi" * 512,  # exceeds 512-byte threshold → stored externally
        activity_input_size=10,
        activity_output_size=10,
        output_size=10,
    )
    expected_input_size = await _expected_payload_size(data_converter, wf_input)

    with LogCapturer().logs_captured(_workflow_logger, level=logging.DEBUG) as capturer:
        async with new_worker(
            client, ExtStoreWorkflow, activities=[ext_store_activity]
        ) as worker:
            await client.execute_workflow(
                ExtStoreWorkflow.run,
                wf_input,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

    records = _tmprl1104_records(capturer)
    assert len(records) == 2

    # WFT 1: retrieves the externalized workflow input
    assert (
        records[0]
        .getMessage()
        .startswith("[TMPRL1104] Workflow task duration information (")
    )
    assert getattr(records[0], "payload_download_count") == 1
    assert getattr(records[0], "payload_download_size") == expected_input_size
    assert getattr(records[0], "payload_download_duration") > timedelta(0)
    assert not hasattr(records[0], "payload_upload_count")

    # WFT 2: activity result is small — no external storage
    assert (
        records[1]
        .getMessage()
        .startswith("[TMPRL1104] Workflow task duration information (")
    )
    assert not hasattr(records[1], "payload_download_count")
    assert not hasattr(records[1], "payload_upload_count")


async def test_tmprl1104_with_extstore_upload(env: WorkflowEnvironment) -> None:
    """When external storage encodes payloads, TMPRL1104 logs include upload
    metrics on the WFT that produces them."""
    driver = InMemoryTestDriver()
    data_converter = dataclasses.replace(
        temporalio.converter.default(),
        external_storage=ExternalStorage(
            drivers=[driver],
            payload_size_threshold=512,
        ),
    )
    client = await Client.connect(
        env.client.service_client.config.target_host,
        namespace=env.client.namespace,
        data_converter=data_converter,
    )

    wf_output = "wo" * 1024  # 2048 bytes → stored externally
    expected_output_size = await _expected_payload_size(data_converter, wf_output)

    with LogCapturer().logs_captured(_workflow_logger, level=logging.DEBUG) as capturer:
        async with new_worker(
            client, ExtStoreWorkflow, activities=[ext_store_activity]
        ) as worker:
            await client.execute_workflow(
                ExtStoreWorkflow.run,
                ExtStoreWorkflowInput(
                    input_data="small",
                    activity_input_size=10,
                    activity_output_size=10,
                    output_size=2048,  # large output → stored externally on completion
                ),
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

    records = _tmprl1104_records(capturer)
    assert len(records) == 2

    # WFT 1: small input — no external storage
    assert (
        records[0]
        .getMessage()
        .startswith("[TMPRL1104] Workflow task duration information (")
    )
    assert not hasattr(records[0], "payload_download_count")
    assert not hasattr(records[0], "payload_upload_count")

    # WFT 2: workflow returns large result → uploaded
    assert (
        records[1]
        .getMessage()
        .startswith("[TMPRL1104] Workflow task duration information (")
    )
    assert not hasattr(records[1], "payload_download_count")
    assert getattr(records[1], "payload_upload_count") == 1
    assert getattr(records[1], "payload_upload_size") == expected_output_size
    assert getattr(records[1], "payload_upload_duration") > timedelta(0)


async def test_tmprl1104_with_extstore_download_and_upload(
    env: WorkflowEnvironment,
) -> None:
    """When both download and upload happen across WFTs, TMPRL1104 logs include
    both sets of metrics."""
    driver = InMemoryTestDriver()
    data_converter = dataclasses.replace(
        temporalio.converter.default(),
        external_storage=ExternalStorage(
            drivers=[driver],
            payload_size_threshold=512,
        ),
    )
    client = await Client.connect(
        env.client.service_client.config.target_host,
        namespace=env.client.namespace,
        data_converter=data_converter,
    )

    wf_input = ExtStoreWorkflowInput(
        input_data="wi" * 512,  # large input → download on first WFT
        activity_input_size=10,
        activity_output_size=10,
        output_size=2048,  # large output → upload on final WFT
    )
    expected_input_size = await _expected_payload_size(data_converter, wf_input)
    wf_output = "wo" * 1024
    expected_output_size = await _expected_payload_size(data_converter, wf_output)

    with LogCapturer().logs_captured(_workflow_logger, level=logging.DEBUG) as capturer:
        async with new_worker(
            client, ExtStoreWorkflow, activities=[ext_store_activity]
        ) as worker:
            await client.execute_workflow(
                ExtStoreWorkflow.run,
                wf_input,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

    records = _tmprl1104_records(capturer)
    assert len(records) == 2

    # WFT 1: retrieves externalized workflow input
    assert (
        records[0]
        .getMessage()
        .startswith("[TMPRL1104] Workflow task duration information (")
    )
    assert getattr(records[0], "payload_download_count") == 1
    assert getattr(records[0], "payload_download_size") == expected_input_size
    assert getattr(records[0], "payload_download_duration") > timedelta(0)
    assert not hasattr(records[0], "payload_upload_count")

    # WFT 2: uploads externalized workflow result
    assert (
        records[1]
        .getMessage()
        .startswith("[TMPRL1104] Workflow task duration information (")
    )
    assert not hasattr(records[1], "payload_download_count")
    assert getattr(records[1], "payload_upload_count") == 1
    assert getattr(records[1], "payload_upload_size") == expected_output_size
    assert getattr(records[1], "payload_upload_duration") > timedelta(0)


# ---------------------------------------------------------------------------
# Store-metadata context tests
# ---------------------------------------------------------------------------


class ContextTrackingStorageDriver(StorageDriver):
    """In-memory driver that records the store context on each store/retrieve."""

    def __init__(self) -> None:
        self._storage: dict[str, bytes] = {}
        self.store_contexts: list[StorageDriverStoreContext] = []

    def name(self) -> str:
        return "context-tracking"

    async def store(
        self,
        context: StorageDriverStoreContext,
        payloads: Sequence[Payload],
    ) -> list[StorageDriverClaim]:
        self.store_contexts.append(context)
        claims: list[StorageDriverClaim] = []
        for payload in payloads:
            key = f"payload-{len(self._storage)}"
            self._storage[key] = payload.SerializeToString()
            claims.append(StorageDriverClaim(claim_data={"key": key}))
        return claims

    async def retrieve(
        self,
        context: StorageDriverRetrieveContext,
        claims: Sequence[StorageDriverClaim],
    ) -> list[Payload]:
        results: list[Payload] = []
        for claim in claims:
            payload = Payload()
            payload.ParseFromString(self._storage[claim.claim_data["key"]])
            results.append(payload)
        return results


@workflow.defn
class SignalWaitWorkflow:
    def __init__(self) -> None:
        self._signal_data: str | None = None

    @workflow.run
    async def run(self, _arg: str) -> str:
        await workflow.wait_condition(lambda: self._signal_data is not None)
        return self._signal_data  # type: ignore

    @workflow.signal
    async def my_signal(self, data: str) -> None:
        self._signal_data = data


@workflow.defn
class EchoWorkflow:
    @workflow.run
    async def run(self, data: str) -> str:
        return data


@workflow.defn
class ChildWorkflowStoreMetadataTestWorkflow:
    @workflow.run
    async def run(self, data: str) -> str:
        return await workflow.execute_child_workflow(
            EchoWorkflow.run,
            data,
            id=f"{workflow.info().workflow_id}-child",
        )


async def _make_tracking_client(
    env: WorkflowEnvironment,
) -> tuple[Client, ContextTrackingStorageDriver]:
    driver = ContextTrackingStorageDriver()
    client = await Client.connect(
        env.client.service_client.config.target_host,
        namespace=env.client.namespace,
        data_converter=dataclasses.replace(
            temporalio.converter.default(),
            external_storage=ExternalStorage(
                drivers=[driver],
                payload_size_threshold=0,
            ),
        ),
    )
    return client, driver


async def test_store_metadata_start_workflow(env: WorkflowEnvironment) -> None:
    """start_workflow should set workflow id and type on store context."""
    client, driver = await _make_tracking_client(env)
    workflow_id = str(uuid.uuid4())

    async with new_worker(client, EchoWorkflow) as worker:
        await client.execute_workflow(
            EchoWorkflow.run,
            "hello",
            id=workflow_id,
            task_queue=worker.task_queue,
        )

    assert len(driver.store_contexts) == 2

    # [0] Workflow input arg
    client_ctx = driver.store_contexts[0]
    assert isinstance(client_ctx.target, StorageDriverWorkflowInfo)
    assert client_ctx.target.namespace == client.namespace
    assert client_ctx.target.id == workflow_id
    assert client_ctx.target.type == "EchoWorkflow"
    assert client_ctx.target.run_id is None

    # [1] Workflow result
    worker_ctx = driver.store_contexts[1]
    assert isinstance(worker_ctx.target, StorageDriverWorkflowInfo)
    assert worker_ctx.target.namespace == client.namespace
    assert worker_ctx.target.id == workflow_id
    assert worker_ctx.target.type == "EchoWorkflow"
    assert worker_ctx.target.run_id is not None


async def test_store_metadata_signal_with_start(env: WorkflowEnvironment) -> None:
    """signal_with_start should set workflow metadata for signal arg encoding."""
    client, driver = await _make_tracking_client(env)
    workflow_id = str(uuid.uuid4())

    async with new_worker(client, SignalWaitWorkflow) as worker:
        handle = await client.start_workflow(
            SignalWaitWorkflow.run,
            "hello",
            id=workflow_id,
            task_queue=worker.task_queue,
            start_signal="my_signal",
            start_signal_args=["signal-data"],
        )
        await handle.result()

    assert len(driver.store_contexts) == 3

    # [0] Workflow input arg
    input_ctx = driver.store_contexts[0]
    assert isinstance(input_ctx.target, StorageDriverWorkflowInfo)
    assert input_ctx.target.id == workflow_id
    assert input_ctx.target.type == "SignalWaitWorkflow"
    assert input_ctx.target.run_id is None

    # [1] Signal arg
    signal_ctx = driver.store_contexts[1]
    assert isinstance(signal_ctx.target, StorageDriverWorkflowInfo)
    assert signal_ctx.target.id == workflow_id
    assert signal_ctx.target.type == "SignalWaitWorkflow"
    assert signal_ctx.target.run_id is None

    # [2] Workflow result
    result_ctx = driver.store_contexts[2]
    assert isinstance(result_ctx.target, StorageDriverWorkflowInfo)
    assert result_ctx.target.id == workflow_id
    assert result_ctx.target.type == "SignalWaitWorkflow"
    assert result_ctx.target.run_id is not None


async def test_store_metadata_signal_workflow(env: WorkflowEnvironment) -> None:
    """signal_workflow should set workflow id on store context."""
    client, driver = await _make_tracking_client(env)
    workflow_id = str(uuid.uuid4())

    async with new_worker(client, SignalWaitWorkflow) as worker:
        handle = await client.start_workflow(
            SignalWaitWorkflow.run,
            "hello",
            id=workflow_id,
            task_queue=worker.task_queue,
        )
        # Signal separately (not signal-with-start)
        await handle.signal(SignalWaitWorkflow.my_signal, "signal-data")
        await handle.result()

    assert len(driver.store_contexts) == 3

    # [0] Client starts workflow
    start_ctx = driver.store_contexts[0]
    assert isinstance(start_ctx.target, StorageDriverWorkflowInfo)
    assert start_ctx.target.id == workflow_id
    assert start_ctx.target.type == "SignalWaitWorkflow"
    assert start_ctx.target.run_id is None

    # [1] Client sends signal: type and run_id are unknown at signal time
    signal_ctx = driver.store_contexts[1]
    assert isinstance(signal_ctx.target, StorageDriverWorkflowInfo)
    assert signal_ctx.target.id == workflow_id
    assert signal_ctx.target.type is None
    assert signal_ctx.target.run_id is None

    # [2] Workflow worker returns result
    result_ctx = driver.store_contexts[2]
    assert isinstance(result_ctx.target, StorageDriverWorkflowInfo)
    assert result_ctx.target.id == workflow_id
    assert result_ctx.target.type == "SignalWaitWorkflow"
    assert result_ctx.target.run_id is not None


async def test_store_metadata_schedule_action(env: WorkflowEnvironment) -> None:
    """Schedule action _to_proto should set workflow metadata."""
    if env.supports_time_skipping:
        pytest.skip("Java test server doesn't support schedules")
    client, driver = await _make_tracking_client(env)
    task_queue = str(uuid.uuid4())
    schedule_id = f"sched-{uuid.uuid4()}"

    try:
        await client.create_schedule(
            schedule_id,
            temporalio.client.Schedule(
                action=temporalio.client.ScheduleActionStartWorkflow(
                    EchoWorkflow.run,
                    "hello",
                    id=f"wf-{schedule_id}",
                    task_queue=task_queue,
                ),
                spec=temporalio.client.ScheduleSpec(),
            ),
        )

        assert len(driver.store_contexts) == 1

        # [0] Client encodes workflow args when creating the schedule action
        ctx = driver.store_contexts[0]
        assert isinstance(ctx.target, StorageDriverWorkflowInfo)
        assert ctx.target.namespace == client.namespace
        assert ctx.target.id == f"wf-{schedule_id}"
        assert ctx.target.type == "EchoWorkflow"
        assert ctx.target.run_id is None
    finally:
        try:
            handle = client.get_schedule_handle(schedule_id)
            await handle.delete()
        except Exception:
            pass


async def test_store_metadata_child_workflow(env: WorkflowEnvironment) -> None:
    """External storage should receive the child workflow as the target when scheduling."""
    client, driver = await _make_tracking_client(env)
    workflow_id = f"workflow-{uuid.uuid4()}"
    child_workflow_id = f"{workflow_id}-child"

    async with new_worker(
        client,
        ChildWorkflowStoreMetadataTestWorkflow,
        EchoWorkflow,
    ) as worker:
        await client.execute_workflow(
            ChildWorkflowStoreMetadataTestWorkflow.run,
            "hello",
            id=workflow_id,
            task_queue=worker.task_queue,
        )

    assert len(driver.store_contexts) == 4

    # [0] Client starts parent workflow
    client_ctx = driver.store_contexts[0]
    assert isinstance(client_ctx.target, StorageDriverWorkflowInfo)
    assert client_ctx.target.id == workflow_id
    assert client_ctx.target.type == "ChildWorkflowStoreMetadataTestWorkflow"
    assert client_ctx.target.run_id is None

    # [1] Parent schedules child: target = child workflow
    start_child_ctx = driver.store_contexts[1]
    assert isinstance(start_child_ctx.target, StorageDriverWorkflowInfo)
    assert start_child_ctx.target.id == child_workflow_id
    assert start_child_ctx.target.type == "EchoWorkflow"
    assert start_child_ctx.target.run_id is None

    # [2] Child returns result: target = parent workflow (child results are
    # stored in the parent's key space so they remain accessible during replay)
    child_result_ctx = driver.store_contexts[2]
    assert isinstance(child_result_ctx.target, StorageDriverWorkflowInfo)
    assert child_result_ctx.target.id == workflow_id
    # ParentInfo does not carry workflow type
    assert child_result_ctx.target.type is None
    assert child_result_ctx.target.run_id is not None

    # [3] Parent returns result: target = parent (current execution)
    parent_result_ctx = driver.store_contexts[3]
    assert isinstance(parent_result_ctx.target, StorageDriverWorkflowInfo)
    assert parent_result_ctx.target.id == workflow_id
    assert parent_result_ctx.target.type == "ChildWorkflowStoreMetadataTestWorkflow"
    assert parent_result_ctx.target.run_id is not None


# Workflow definitions for gap tests


@activity.defn
async def echo_activity(input: str) -> str:
    """Simple activity that returns its input."""
    return input


@workflow.defn
class ActivityScheduleMetadataWorkflow:
    """Workflow that schedules an activity to test activity metadata on the store context."""

    @workflow.run
    async def run(self, data: str) -> str:
        return await workflow.execute_activity(
            echo_activity,
            data,
            activity_id="my-activity-id",
            schedule_to_close_timeout=timedelta(seconds=10),
        )


@workflow.defn
class SignalExternalMetadataWorkflow:
    """Workflow that signals another workflow."""

    @workflow.run
    async def run(self, target_workflow_id: str) -> None:
        await workflow.get_external_workflow_handle(target_workflow_id).signal(
            SignalWaitWorkflow.my_signal, "signal-from-workflow"
        )


async def test_store_metadata_activity_scheduling(env: WorkflowEnvironment) -> None:
    """When a workflow schedules an activity, context.activity should be populated."""
    client, driver = await _make_tracking_client(env)
    workflow_id = f"workflow-{uuid.uuid4()}"

    async with new_worker(
        client,
        ActivityScheduleMetadataWorkflow,
        activities=[echo_activity],
    ) as worker:
        await client.execute_workflow(
            ActivityScheduleMetadataWorkflow.run,
            "hello",
            id=workflow_id,
            task_queue=worker.task_queue,
        )

    assert len(driver.store_contexts) == 4

    # [0] Client starts workflow
    client_ctx = driver.store_contexts[0]
    assert isinstance(client_ctx.target, StorageDriverWorkflowInfo)
    assert client_ctx.target.id == workflow_id
    assert client_ctx.target.type == "ActivityScheduleMetadataWorkflow"
    assert client_ctx.target.run_id is None

    # [1] Workflow worker schedules activity
    schedule_ctx = driver.store_contexts[1]
    assert isinstance(schedule_ctx.target, StorageDriverWorkflowInfo)
    assert schedule_ctx.target.namespace == client.namespace
    assert schedule_ctx.target.id == workflow_id
    assert schedule_ctx.target.type == "ActivityScheduleMetadataWorkflow"
    assert schedule_ctx.target.run_id is not None

    # [2] Activity worker completes
    execute_ctx = driver.store_contexts[2]
    assert isinstance(execute_ctx.target, StorageDriverWorkflowInfo)
    assert execute_ctx.target.namespace == client.namespace
    assert execute_ctx.target.id == workflow_id
    assert execute_ctx.target.type == "ActivityScheduleMetadataWorkflow"
    assert execute_ctx.target.run_id is not None

    # [3] Workflow returns result
    result_ctx = driver.store_contexts[3]
    assert isinstance(result_ctx.target, StorageDriverWorkflowInfo)
    assert result_ctx.target.id == workflow_id
    assert result_ctx.target.type == "ActivityScheduleMetadataWorkflow"
    assert result_ctx.target.run_id is not None


async def test_store_metadata_signal_external_workflow(
    env: WorkflowEnvironment,
) -> None:
    """Signaling an external workflow should set workflow.id to the target."""
    client, driver = await _make_tracking_client(env)
    target_workflow_id = f"target-{uuid.uuid4()}"
    sender_workflow_id = f"sender-{uuid.uuid4()}"

    async with new_worker(
        client,
        SignalExternalMetadataWorkflow,
        SignalWaitWorkflow,
    ) as worker:
        # Start the target workflow first
        target_handle = await client.start_workflow(
            SignalWaitWorkflow.run,
            "waiting",
            id=target_workflow_id,
            task_queue=worker.task_queue,
        )
        # Start the sender which will signal the target
        await client.execute_workflow(
            SignalExternalMetadataWorkflow.run,
            target_workflow_id,
            id=sender_workflow_id,
            task_queue=worker.task_queue,
        )
        await target_handle.result()

    assert len(driver.store_contexts) == 5

    # [0] Client starts target workflow (SignalWaitWorkflow)
    target_start_ctx = driver.store_contexts[0]
    assert isinstance(target_start_ctx.target, StorageDriverWorkflowInfo)
    assert target_start_ctx.target.id == target_workflow_id
    assert target_start_ctx.target.type == "SignalWaitWorkflow"
    assert target_start_ctx.target.run_id is None

    # [1] Client starts sender workflow (SignalExternalMetadataWorkflow)
    sender_start_ctx = driver.store_contexts[1]
    assert isinstance(sender_start_ctx.target, StorageDriverWorkflowInfo)
    assert sender_start_ctx.target.id == sender_workflow_id
    assert sender_start_ctx.target.type == "SignalExternalMetadataWorkflow"
    assert sender_start_ctx.target.run_id is None

    # [2] Sender signals target: target = the workflow being signaled
    signal_ctx = driver.store_contexts[2]
    assert isinstance(signal_ctx.target, StorageDriverWorkflowInfo)
    assert signal_ctx.target.id == target_workflow_id
    assert signal_ctx.target.type is None
    assert signal_ctx.target.run_id is None

    # [3] and [4] are the sender and target workflow completions in some order.
    # The sender's WFT 2 (after signal resolution) and the target's WFT (after
    # receiving the signal) are both scheduled by the server at nearly the same
    # time, so the order of their completions is non-deterministic.
    completion_ctxs = {
        ctx.target.id: ctx
        for ctx in driver.store_contexts[3:5]
        if isinstance(ctx.target, StorageDriverWorkflowInfo) and ctx.target.id
    }
    assert sender_workflow_id in completion_ctxs
    assert target_workflow_id in completion_ctxs

    sender_result_ctx = completion_ctxs[sender_workflow_id]
    assert isinstance(sender_result_ctx.target, StorageDriverWorkflowInfo)
    assert sender_result_ctx.target.run_id is not None

    target_result_ctx = completion_ctxs[target_workflow_id]
    assert isinstance(target_result_ctx.target, StorageDriverWorkflowInfo)
    assert target_result_ctx.target.run_id is not None


async def test_store_metadata_standalone_activity(env: WorkflowEnvironment) -> None:
    """Standalone activity worker should use StorageDriverActivityInfo as target."""
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )
    client, driver = await _make_tracking_client(env)
    activity_id = f"activity-{uuid.uuid4()}"

    async with new_worker(client, activities=[echo_activity]) as worker:
        await client.execute_activity(
            echo_activity,
            "hello",
            id=activity_id,
            task_queue=worker.task_queue,
            schedule_to_close_timeout=timedelta(seconds=30),
        )

    assert len(driver.store_contexts) == 2

    client_ctx = driver.store_contexts[0]
    # [0] Client schedules standalone activity
    assert isinstance(client_ctx.target, StorageDriverActivityInfo)
    assert client_ctx.target.namespace == client.namespace
    assert client_ctx.target.id == activity_id
    assert client_ctx.target.type == "echo_activity"
    assert client_ctx.target.run_id is None

    # [1] Activity worker completes: target = activity (no parent workflow)
    execute_ctx = driver.store_contexts[1]
    assert isinstance(execute_ctx.target, StorageDriverActivityInfo)
    assert execute_ctx.target.namespace == client.namespace
    assert execute_ctx.target.id == activity_id
    assert execute_ctx.target.type == "echo_activity"
    assert execute_ctx.target.run_id is not None


@workflow.defn
class ContinueAsNewExtStoreWorkflow:
    """Workflow that continues-as-new once with a large payload.

    Run 1: called with large_payload, calls continue_as_new with same payload.
    Run 2: called with large_payload again (from CaN), returns immediately.
    """

    @workflow.run
    async def run(self, large_payload: str) -> str:
        if workflow.info().continued_run_id is None:
            workflow.continue_as_new(large_payload)
        return "done"


async def test_extstore_continue_as_new_result_stored_under_current_run(
    env: WorkflowEnvironment,
) -> None:
    """A CaN continuation's result payloads are stored under the continuation's
    own run_id, not under the originating run's run_id.
    """
    client, driver = await _make_tracking_client(env)

    async with new_worker(client, ContinueAsNewExtStoreWorkflow) as worker:
        handle = await client.start_workflow(
            ContinueAsNewExtStoreWorkflow.run,
            "x" * 1024,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        first_run_id = (await handle.describe()).run_id
        await handle.result()
        last_run_id = (await handle.describe()).run_id
    assert len(driver.store_contexts) == 3

    # [0] Client starts workflow
    client_ctx = driver.store_contexts[0]
    assert isinstance(client_ctx.target, StorageDriverWorkflowInfo)
    assert client_ctx.target.run_id is None

    # [1] Workflow 1 encodes CaN args
    can_args_ctx = driver.store_contexts[1]
    assert isinstance(can_args_ctx.target, StorageDriverWorkflowInfo)
    assert can_args_ctx.target.run_id == first_run_id

    # [2] Workflow 2 encodes result in its own context
    result_ctx = driver.store_contexts[2]
    assert isinstance(result_ctx.target, StorageDriverWorkflowInfo)
    assert result_ctx.target.run_id is not None
    assert result_ctx.target.run_id == last_run_id
