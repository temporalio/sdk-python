"""Workflow and activity definitions for test_s3driver.py integration tests.

Kept in a separate module so the workflow sandbox does not encounter
aioboto3/aiobotocore/botocore/urllib3 imports when preparing workflow classes.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

LARGE = "x" * 356  # ~358 bytes as a JSON string, above the 256-byte test threshold
LARGE_2 = "y" * 356  # distinct large payload with a different SHA-256 hash


@activity.defn
async def large_io_activity(_data: str) -> str:
    return LARGE


@activity.defn
async def large_output_activity() -> str:
    """Returns a large payload with no retries; used to test S3 store failures."""
    return LARGE


@workflow.defn
class LargeOutputNoRetryWorkflow:
    """Executes a single activity that returns a large payload with no retries.

    Used to verify that S3 store failures surface in workflow history without
    retries masking the error.
    """

    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_activity(
            large_output_activity,
            schedule_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


@workflow.defn
class LargeIOWorkflow:
    """Passes its input to an activity and returns a large output."""

    @workflow.run
    async def run(self, data: str) -> str:
        await workflow.execute_activity(
            large_io_activity,
            data,
            schedule_to_close_timeout=timedelta(seconds=10),
        )
        return LARGE


@activity.defn
async def download_document(document_id: str) -> str:
    """Downloads the raw document content from remote storage."""
    del document_id
    return LARGE  # simulates a large raw document


@activity.defn
async def extract_text(raw_content: str) -> str:
    """Extracts and normalizes text from the raw document content."""
    del raw_content
    return LARGE_2  # simulates extracted text — different content, different hash


@activity.defn
async def index_document(text: str) -> str:
    """Indexes the extracted text into the search index. Returns the index record ID."""
    del text
    return "idx-00001"  # small confirmation — not offloaded to external storage


@workflow.defn
class DocumentIngestionWorkflow:
    """Downloads a document, extracts its text, and indexes it for search.

    Illustrates how large intermediate payloads (raw document content, extracted
    text) are transparently offloaded to S3 between activity boundaries without
    any special handling in the workflow code.
    """

    @workflow.run
    async def run(self, document_id: str) -> str:
        raw_content = await workflow.execute_activity(
            download_document,
            document_id,
            schedule_to_close_timeout=timedelta(seconds=10),
        )
        extracted_text = await workflow.execute_activity(
            extract_text,
            raw_content,
            schedule_to_close_timeout=timedelta(seconds=10),
        )
        return await workflow.execute_activity(
            index_document,
            extracted_text,
            schedule_to_close_timeout=timedelta(seconds=10),
        )


@workflow.defn
class ChildWorkflow:
    @workflow.run
    async def run(self, data: str) -> str:
        return f"{len(data)}"


@workflow.defn
class ParentWithChildWorkflow:
    """Delegates work to a child workflow whose ID is {parent_id}-child."""

    @workflow.run
    async def run(self) -> str:
        child_id = f"{workflow.info().workflow_id}-child"
        return await workflow.execute_child_workflow(
            ChildWorkflow.run,
            LARGE,
            id=child_id,
            execution_timeout=timedelta(seconds=10),
        )


@workflow.defn
class PaymentProcessingWorkflow:
    """Processes payment for an order and returns a large payment confirmation.

    Intended to be spawned as a child of OrderFulfillmentWorkflow.
    """

    @workflow.run
    async def run(self, order_details: str) -> str:
        del order_details
        return LARGE_2  # payment confirmation


@workflow.defn
class OrderFulfillmentWorkflow:
    """Coordinates order fulfillment by delegating payment to a child workflow.

    Passes the large order details to a PaymentProcessingWorkflow child whose ID
    is {parent_id}-payment, then returns the child's payment confirmation.
    """

    @workflow.run
    async def run(self, order_details: str) -> str:
        payment_id = f"{workflow.info().workflow_id}-payment"
        return await workflow.execute_child_workflow(
            PaymentProcessingWorkflow.run,
            order_details,
            id=payment_id,
            execution_timeout=timedelta(seconds=10),
        )


@workflow.defn
class ModelTrainingWorkflow:
    """Simulates a long-running ML training job.

    Accepts a large training config as input, allows the caller to inject
    override parameters mid-run via signal, and exposes intermediate metrics
    via an update. Demonstrates that large payloads crossing all three
    primitive boundaries (input, signal arg, update result) are stored under
    the same workflow ID prefix in S3.
    """

    def __init__(self) -> None:
        self._overrides_received = False
        self._done = False

    @workflow.run
    async def run(self, training_config: str) -> str:
        del training_config
        await workflow.wait_condition(lambda: self._done)
        return LARGE  # final training summary

    @workflow.signal
    async def apply_overrides(self, override_params: str) -> None:
        """Injects updated hyperparameters into the running training job."""
        del override_params
        self._overrides_received = True

    @workflow.signal
    async def complete(self) -> None:
        self._done = True

    @workflow.update
    async def get_metrics(self, checkpoint_id: str) -> str:
        """Returns the current training metrics snapshot."""
        del checkpoint_id
        return LARGE_2  # large metrics payload


@workflow.defn
class SignalQueryUpdateWorkflow:
    """Long-running workflow that accepts a signal, query, and update."""

    def __init__(self) -> None:
        self._done = False

    @workflow.run
    async def run(self) -> str:
        await workflow.wait_condition(lambda: self._done)
        return LARGE

    @workflow.signal
    async def finish(self, _data: str) -> None:
        self._done = True

    @workflow.query
    def get_value(self, _data: str) -> str:
        return LARGE

    @workflow.update
    async def do_update(self, _data: str) -> str:
        return LARGE
