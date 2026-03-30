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
    assert len(keys) == 1
    assert f"/ns/default/wfi/{workflow_id}/d/sha256/" in keys[0]


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
    assert len(keys) == 1
    assert f"/ns/default/wfi/{workflow_id}/d/sha256/" in keys[0]


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
    assert len(keys) == 1
    assert f"/ns/default/wfi/{workflow_id}/" in keys[0]
    assert (
        "/aci/" not in keys[0]
    ), "Activity input should use workflow_id, not activity_id"


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
    assert len(keys) == 1
    assert f"/ns/default/wfi/{workflow_id}/d/sha256/" in keys[0]


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
    assert len(keys) == 1
    assert f"/ns/default/wfi/{workflow_id}/d/sha256/" in keys[0]


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
    assert len(keys) == 1
    assert f"/ns/default/wfi/{workflow_id}/d/sha256/" in keys[0]


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
    assert len(keys) == 1
    assert f"/ns/default/wfi/{workflow_id}/d/sha256/" in keys[0]


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
    assert len(keys) == 1
    child_workflow_id = f"{workflow_id}-child"
    assert f"/ns/default/wfi/{child_workflow_id}/d/sha256/" in keys[0]


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
    assert len(keys) == 1
    assert "/ns/default/" in keys[0], "Namespace segment should be present"
    assert (
        f"/wfi/{workflow_id}/" in keys[0]
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
    assert len(keys) == 2
    assert f"/ns/default/wfi/{workflow_id}/d/sha256/" in keys[0]
    assert f"/ns/default/wfi/{workflow_id}/d/sha256/" in keys[1]
    assert keys[0] != keys[1]


async def test_s3_driver_single_workflow_same_key_namespace(
    tmprl_client: Client, aioboto3_client: S3Client
) -> None:
    """A training job started with a large config, injected with large override
    parameters mid-run, and polled for large metrics — all produce S3 keys
    under the same workflow ID prefix, regardless of which primitive carried
    the payload."""
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
    # LARGE (input + signal arg) and LARGE_2 (metrics result) deduplicate to
    # two distinct keys — both anchored under the same workflow ID prefix.
    assert len(keys) == 2
    assert all(f"/ns/default/wfi/{workflow_id}/" in key for key in keys)


async def test_s3_driver_parent_child_independent_key_namespaces(
    tmprl_client: Client, aioboto3_client: S3Client
) -> None:
    """An order fulfillment workflow spawns a child payment processor, passes it
    a large order payload, and returns the child's large payment confirmation.
    Each workflow accumulates S3 keys under its own workflow ID prefix —
    parent and child key namespaces are fully independent."""
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
    parent_prefix = f"/ns/default/wfi/{workflow_id}/d/"
    child_prefix = f"/ns/default/wfi/{payment_id}/d/"
    parent_keys = [k for k in keys if parent_prefix in k]
    child_keys = [k for k in keys if child_prefix in k]
    # The parent stores its input (LARGE) and the child's result propagated
    # back (LARGE_2) under the parent's prefix → 2 keys.
    # The child stores its input (LARGE) and its result (LARGE_2) under the
    # child's prefix → 2 keys.
    assert len(parent_keys) == 2
    assert len(child_keys) == 2


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
    expected_key = f"v0/ns/default/wfi/{workflow_id}/d/sha256/{expected_hash}"

    assert isinstance(exc_info.value, WorkflowFailureError)
    activity_error = exc_info.value.__cause__
    assert isinstance(activity_error, ActivityError)
    app_error = activity_error.__cause__
    assert isinstance(app_error, ApplicationError)
    assert app_error.type == "RuntimeError"
    assert (
        app_error.message
        == f"S3StorageDriver store failed [bucket={bad_bucket}, key={expected_key}]"
    )
