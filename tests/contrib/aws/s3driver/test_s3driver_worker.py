"""Worker integration tests for S3StorageDriver key structure.

Runs real Temporal workflows against a real worker (backed by a moto S3
server) and asserts the S3 object key structure produced for each Temporal
primitive: workflow input/output, activity input/output, signals, queries,
updates, and child workflows.
"""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import aioboto3
import pytest
from types_aiobotocore_s3.client import S3Client

import temporalio.converter
from temporalio.client import Client, WorkflowFailureError
from temporalio.contrib.aws.s3driver import S3StorageDriver
from temporalio.contrib.aws.s3driver.aioboto3 import new_aioboto3_client
from temporalio.converter import ExternalStorage, JSONPlainPayloadConverter
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.testing import WorkflowEnvironment
from tests.contrib.aws.s3driver.conftest import BUCKET, REGION
from tests.contrib.aws.s3driver.workflows import (
    LARGE,
    ChildWorkflow,
    DocumentIngestionWorkflow,
    LargeIOWorkflow,
    LargeOutputNoRetryWorkflow,
    ModelTrainingWorkflow,
    OrderFulfillmentWorkflow,
    ParentWithChildWorkflow,
    PaymentProcessingWorkflow,
    SignalQueryUpdateWorkflow,
    download_document,
    extract_text,
    index_document,
    large_io_activity,
    large_output_activity,
)
from tests.helpers import new_worker

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_THRESHOLD = 256  # bytes — low so all test payloads are offloaded

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def tmprl_client(
    env: WorkflowEnvironment, aioboto3_client: S3Client
) -> AsyncIterator[Client]:
    """Temporal client wired with ExternalStorage backed by the moto S3 server."""
    driver = S3StorageDriver(client=new_aioboto3_client(aioboto3_client), bucket=BUCKET)
    yield await Client.connect(
        env.client.service_client.config.target_host,
        namespace=env.client.namespace,
        data_converter=dataclasses.replace(
            temporalio.converter.default(),
            external_storage=ExternalStorage(
                drivers=[driver],
                payload_size_threshold=_THRESHOLD,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def _list_keys(aioboto3_client: S3Client) -> list[str]:
    resp = await aioboto3_client.list_objects_v2(Bucket=BUCKET)
    return sorted(
        key for obj in resp.get("Contents", []) if (key := obj.get("Key")) is not None
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_s3_driver_workflow_input_key(
    tmprl_client: Client, aioboto3_client: S3Client
) -> None:
    workflow_id = str(uuid.uuid4())
    async with new_worker(
        tmprl_client, LargeIOWorkflow, activities=[large_io_activity]
    ) as worker:
        await tmprl_client.execute_workflow(
            LargeIOWorkflow.run,
            LARGE,
            id=workflow_id,
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=5),
        )
    keys = await _list_keys(aioboto3_client)

    # Client stores workflow input with ri=null (run ID not yet assigned);
    # worker stores activity input with ri=run_id — same bytes, two S3 objects.
    assert len(keys) == 2
    assert all(
        f"/ns/default/wt/LargeIOWorkflow/wi/{workflow_id}/ri/" in k for k in keys
    )
    # Client-side store: ri=null because run ID is not yet known.
    assert sum(1 for k in keys if "/ri/null/" in k) == 1
    # Worker-side store: ri=run_id, assigned by the server.
    assert sum(1 for k in keys if "/ri/null/" not in k) == 1


async def test_s3_driver_workflow_output_key(
    tmprl_client: Client, aioboto3_client: S3Client
) -> None:
    workflow_id = str(uuid.uuid4())
    async with new_worker(
        tmprl_client, LargeIOWorkflow, activities=[large_io_activity]
    ) as worker:
        result = await tmprl_client.execute_workflow(
            LargeIOWorkflow.run,
            "small",  # small input stays inline; workflow returns LARGE
            id=workflow_id,
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=5),
        )
    assert result == LARGE
    keys = await _list_keys(aioboto3_client)
    # Activity result and workflow result dedup to same key
    assert len(keys) == 1
    assert f"/ns/default/wt/LargeIOWorkflow/wi/{workflow_id}/ri/" in keys[0]
    # Run ID is known for both activity completion and workflow completion
    assert "/ri/null/" not in keys[0]


async def test_s3_driver_workflow_activity_input_key(
    tmprl_client: Client, aioboto3_client: S3Client
) -> None:
    workflow_id = str(uuid.uuid4())
    async with new_worker(
        tmprl_client, LargeIOWorkflow, activities=[large_io_activity]
    ) as worker:
        await tmprl_client.execute_workflow(
            LargeIOWorkflow.run,
            LARGE,  # passed through as the activity's input
            id=workflow_id,
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=5),
        )
    keys = await _list_keys(aioboto3_client)
    # Client start (ri=null) + worker schedules activity (ri=run_id) — same bytes, two objects.
    assert len(keys) == 2
    # Both keys are under the workflow wi/ri prefix, not the activity.
    assert all(
        f"/ns/default/wt/LargeIOWorkflow/wi/{workflow_id}/ri/" in k for k in keys
    )
    # Activity input is keyed under the scheduling workflow, not the activity.
    assert all("/ai/" not in k for k in keys)


async def test_s3_driver_workflow_activity_output_key(
    tmprl_client: Client, aioboto3_client: S3Client
) -> None:
    workflow_id = str(uuid.uuid4())
    async with new_worker(
        tmprl_client, LargeIOWorkflow, activities=[large_io_activity]
    ) as worker:
        await tmprl_client.execute_workflow(
            LargeIOWorkflow.run,
            "small",  # small input; activity returns LARGE
            id=workflow_id,
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=5),
        )
    keys = await _list_keys(aioboto3_client)
    # Activity result and workflow result are both LARGE so they deduplicate to one object.
    assert len(keys) == 1
    assert f"/ns/default/wt/LargeIOWorkflow/wi/{workflow_id}/ri/" in keys[0]
    # ri=run_id for both stores (run ID is known by the time the activity completes).
    assert "/ri/null/" not in keys[0]


async def test_s3_driver_standalone_activity_input_key(
    env: WorkflowEnvironment, tmprl_client: Client, aioboto3_client: S3Client
) -> None:
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )
    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())
    async with new_worker(
        tmprl_client, activities=[large_io_activity], task_queue=task_queue
    ):
        await tmprl_client.execute_activity(
            large_io_activity,
            LARGE,
            id=activity_id,
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=5),
        )
    keys = await _list_keys(aioboto3_client)
    # Input and output are the same LARGE bytes but stored under different keys.
    assert len(keys) == 2
    # Both keyed under the activity, not a workflow.
    assert all(
        f"/ns/default/at/large_io_activity/ai/{activity_id}/ri/" in k for k in keys
    )
    assert all("/wt/" not in k for k in keys)
    # Client-side store does not have run ID information
    assert sum(1 for k in keys if "/ri/null/" in k) == 1
    # Worker-side store does have run ID information
    assert sum(1 for k in keys if "/ri/null/" not in k) == 1


async def test_s3_driver_standalone_activity_output_key(
    env: WorkflowEnvironment, tmprl_client: Client, aioboto3_client: S3Client
) -> None:
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )
    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())
    async with new_worker(
        tmprl_client, activities=[large_output_activity], task_queue=task_queue
    ):
        await tmprl_client.execute_activity(
            large_output_activity,
            id=activity_id,
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=5),
        )
    keys = await _list_keys(aioboto3_client)
    # Only the output is large; keyed under the activity.
    assert len(keys) == 1
    assert f"/ns/default/at/large_output_activity/ai/{activity_id}/ri/" in keys[0]
    assert "/ri/null/" not in keys[0]
    assert "/wt/" not in keys[0]


async def test_s3_driver_signal_arg_key(
    tmprl_client: Client, aioboto3_client: S3Client
) -> None:
    workflow_id = str(uuid.uuid4())
    async with new_worker(tmprl_client, SignalQueryUpdateWorkflow) as worker:
        handle = await tmprl_client.start_workflow(
            SignalQueryUpdateWorkflow.run,
            id=workflow_id,
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=5),
        )
        await handle.signal(SignalQueryUpdateWorkflow.finish, LARGE)
        await handle.result()
    keys = await _list_keys(aioboto3_client)
    # Signal arg + workflow result — two distinct keys (different wt and ri).
    assert len(keys) == 2
    # Signal arg: client stores with wt=null (type not known) and ri=null.
    assert any(f"/wt/null/wi/{workflow_id}/ri/null/" in k for k in keys)
    # Workflow result: worker stores with real type and ri=run_id.
    assert any(f"/wt/SignalQueryUpdateWorkflow/wi/{workflow_id}/" in k for k in keys)


async def test_s3_driver_query_result_key(
    tmprl_client: Client, aioboto3_client: S3Client
) -> None:
    workflow_id = str(uuid.uuid4())
    async with new_worker(tmprl_client, SignalQueryUpdateWorkflow) as worker:
        handle = await tmprl_client.start_workflow(
            SignalQueryUpdateWorkflow.run,
            id=workflow_id,
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=5),
        )
        result = await handle.query(SignalQueryUpdateWorkflow.get_value, LARGE)
        assert result == LARGE
        await handle.signal(SignalQueryUpdateWorkflow.finish, "done")
        await handle.result()
    keys = await _list_keys(aioboto3_client)
    # Query arg + (query result deduplicated with workflow result) — two distinct keys.
    assert len(keys) == 2
    # Query arg: client stores with wt=null (type not known) and ri=null.
    assert any(f"/wt/null/wi/{workflow_id}/ri/null/" in k for k in keys)
    # Query result and workflow result are both LARGE and deduplicate to one key with ri=run_id.
    assert any(f"/wt/SignalQueryUpdateWorkflow/wi/{workflow_id}/" in k for k in keys)


async def test_s3_driver_update_result_key(
    tmprl_client: Client, aioboto3_client: S3Client
) -> None:
    workflow_id = str(uuid.uuid4())
    async with new_worker(tmprl_client, SignalQueryUpdateWorkflow) as worker:
        handle = await tmprl_client.start_workflow(
            SignalQueryUpdateWorkflow.run,
            id=workflow_id,
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=5),
        )
        result = await handle.execute_update(SignalQueryUpdateWorkflow.do_update, LARGE)
        assert result == LARGE
        await handle.signal(SignalQueryUpdateWorkflow.finish, "done")
        await handle.result()
    keys = await _list_keys(aioboto3_client)
    # Update arg + (update result deduplicated with workflow result) — two distinct keys.
    assert len(keys) == 2
    # Update arg: client stores with wt=null (type not known) and ri=null.
    assert any(f"/wt/null/wi/{workflow_id}/ri/null/" in k for k in keys)
    # Update result and workflow result are both LARGE and deduplicate to one key with ri=run_id.
    assert any(f"/wt/SignalQueryUpdateWorkflow/wi/{workflow_id}/" in k for k in keys)


async def test_s3_driver_child_workflow_input_key(
    tmprl_client: Client, aioboto3_client: S3Client
) -> None:
    workflow_id = str(uuid.uuid4())
    async with new_worker(
        tmprl_client, ParentWithChildWorkflow, ChildWorkflow
    ) as worker:
        await tmprl_client.execute_workflow(
            ParentWithChildWorkflow.run,
            id=workflow_id,
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=5),
        )
    keys = await _list_keys(aioboto3_client)
    child_workflow_id = f"{workflow_id}-child"
    # Child input is the only large payload — stored under the child's wi/ri.
    assert len(keys) == 1
    # Keyed under the child: child input is stored in the child's context.
    assert f"/ns/default/wt/ChildWorkflow/wi/{child_workflow_id}/ri/" in keys[0]


async def test_s3_driver_identified_casing(
    tmprl_client: Client, aioboto3_client: S3Client
) -> None:
    workflow_id = f"MyWorkflow-{uuid.uuid4()}"
    async with new_worker(
        tmprl_client, LargeIOWorkflow, activities=[large_io_activity]
    ) as worker:
        await tmprl_client.execute_workflow(
            LargeIOWorkflow.run,
            LARGE,
            id=workflow_id,
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=5),
        )
    keys = await _list_keys(aioboto3_client)
    # Client start (ri=null) + worker stores (ri=run_id) — two objects.
    assert len(keys) == 2
    # Workflow ID is percent-encoded but casing is preserved verbatim.
    assert all(
        f"/ns/default/wt/LargeIOWorkflow/wi/{workflow_id}/ri/" in k for k in keys
    ), "Workflow ID should preserve original case in the key"


async def test_s3_driver_content_dedup(
    tmprl_client: Client, aioboto3_client: S3Client
) -> None:
    """Document ingestion produces exactly two distinct S3 keys, even though
    the payloads are repeatedly passed to different activities."""
    workflow_id = str(uuid.uuid4())
    async with new_worker(
        tmprl_client,
        DocumentIngestionWorkflow,
        activities=[download_document, extract_text, index_document],
    ) as worker:
        await tmprl_client.execute_workflow(
            DocumentIngestionWorkflow.run,
            "doc-001",
            id=workflow_id,
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=5),
        )
    keys = await _list_keys(aioboto3_client)
    # Two distinct content hashes (LARGE from download, LARGE_2 from extract) → two keys.
    assert len(keys) == 2
    # Both are under the same workflow wi/ri prefix despite crossing activity boundaries.
    assert all(
        f"/ns/default/wt/DocumentIngestionWorkflow/wi/{workflow_id}/ri/" in k
        for k in keys
    )
    # The two keys differ by content hash only.
    assert keys[0] != keys[1]


async def test_s3_driver_single_workflow_same_key_namespace(
    tmprl_client: Client, aioboto3_client: S3Client
) -> None:
    """A training job started with a large config, injected with large override
    parameters mid-run, and polled for large metrics — all produce S3 keys
    containing the same workflow ID."""
    workflow_id = str(uuid.uuid4())
    async with new_worker(tmprl_client, ModelTrainingWorkflow) as worker:
        handle = await tmprl_client.start_workflow(
            ModelTrainingWorkflow.run,
            LARGE,  # large training config as workflow input
            id=workflow_id,
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=5),
        )
        metrics = await handle.execute_update(
            ModelTrainingWorkflow.get_metrics, "checkpoint-1"
        )
        assert metrics is not None
        await handle.signal(ModelTrainingWorkflow.apply_overrides, LARGE)
        await handle.signal(ModelTrainingWorkflow.complete)
        await handle.result()
    keys = await _list_keys(aioboto3_client)
    # Four distinct keys: client start, signal arg, update result, workflow result.
    assert len(keys) == 4
    # All keys are anchored under the same workflow ID regardless of which primitive carried the payload.
    assert all(f"/wi/{workflow_id}/" in k for k in keys)


async def test_s3_driver_parent_child_independent_key_namespaces(
    tmprl_client: Client, aioboto3_client: S3Client
) -> None:
    """An order fulfillment workflow spawns a child payment processor and passes
    it a large order payload. Child input is keyed under the parent (it lives in
    the parent's history); child output is keyed under the parent (for lifecycle
    resilience — the child result lives in the parent's completion history)."""
    workflow_id = str(uuid.uuid4())
    payment_id = f"{workflow_id}-payment"
    async with new_worker(
        tmprl_client, OrderFulfillmentWorkflow, PaymentProcessingWorkflow
    ) as worker:
        await tmprl_client.execute_workflow(
            OrderFulfillmentWorkflow.run,
            LARGE,  # large order details passed to parent and forwarded to child
            id=workflow_id,
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=5),
        )
    keys = await _list_keys(aioboto3_client)
    parent_keys = [k for k in keys if f"/wi/{workflow_id}/" in k]
    child_keys = [k for k in keys if f"/wi/{payment_id}/" in k]
    # Parent accumulates 3 keys:
    #   1. Client start stored in parent's key space (ri=null)
    #   2. Child result stored in parent's key space
    #   3. Parent's own workflow result
    assert len(parent_keys) == 3
    # Child accumulates 1 key: its input from the parent
    assert len(child_keys) == 1


async def test_s3_store_failure_surfaces_in_workflow_history(
    env: WorkflowEnvironment, moto_server_url: str
) -> None:
    """Verifies that an S3 store failure (nonexistent bucket) produces a
    RuntimeError with bucket and key context that is visible in Temporal
    workflow history via the WorkflowFailureError cause chain."""
    bad_bucket = "nonexistent-bucket"
    session = aioboto3.Session()
    async with session.client(
        "s3",
        region_name=REGION,
        endpoint_url=moto_server_url,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    ) as client:
        driver = S3StorageDriver(client=new_aioboto3_client(client), bucket=bad_bucket)
        bad_client = await Client.connect(
            env.client.service_client.config.target_host,
            namespace=env.client.namespace,
            data_converter=dataclasses.replace(
                temporalio.converter.default(),
                external_storage=ExternalStorage(
                    drivers=[driver],
                    payload_size_threshold=_THRESHOLD,
                ),
            ),
        )
        workflow_id = str(uuid.uuid4())
        async with new_worker(
            bad_client, LargeOutputNoRetryWorkflow, activities=[large_output_activity]
        ) as worker:
            with pytest.raises(WorkflowFailureError) as exc_info:
                await bad_client.execute_workflow(
                    LargeOutputNoRetryWorkflow.run,
                    id=workflow_id,
                    task_queue=worker.task_queue,
                    execution_timeout=timedelta(seconds=5),
                )

    large_payload = JSONPlainPayloadConverter().to_payload(LARGE)
    assert large_payload is not None
    expected_hash = hashlib.sha256(large_payload.SerializeToString()).hexdigest()

    assert isinstance(exc_info.value, WorkflowFailureError)
    activity_error = exc_info.value.__cause__
    assert isinstance(activity_error, ActivityError)
    app_error = activity_error.__cause__
    assert isinstance(app_error, ApplicationError)
    assert app_error.type == "RuntimeError"
    # Key includes run_id which is only known at runtime; use substring checks.
    msg = app_error.message
    assert f"S3StorageDriver store failed [bucket={bad_bucket}, key=" in msg
    assert f"/wt/LargeOutputNoRetryWorkflow/wi/{workflow_id}/ri/" in msg
    assert f"/d/sha256/{expected_hash}]" in msg
