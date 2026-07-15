import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

import pytest

from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.converter import DataConverter, PayloadLimitsConfig
from temporalio.exceptions import (
    TerminatedError,
    TimeoutError,
    TimeoutType,
)
from temporalio.runtime import (
    LogForwardingConfig,
    LoggingConfig,
    Runtime,
    TelemetryConfig,
    TelemetryFilter,
)
from temporalio.testing._workflow import WorkflowEnvironment
from tests import DEV_SERVER_DOWNLOAD_VERSION
from tests.helpers import LogCapturer, assert_eventually, new_worker

# Payload/memo size-limit enforcement lives in sdk-rust. These tests only assert that the SDK's
# plumbing reaches core: oversized completions are failed proactively, the worker opt-out lets
# oversized payloads through to the server, and the connection's warn threshold produces a
# forwarded [TMPRL1103] warning.


@dataclass
class LargePayloadWorkflowInput:
    activity_input_data_size: int
    workflow_output_data_size: int


@dataclass
class LargePayloadActivityInput:
    data: str


@activity.defn
async def large_payload_activity(_input: LargePayloadActivityInput) -> None:
    return None


@workflow.defn
class LargePayloadWorkflow:
    @workflow.run
    async def run(self, input: LargePayloadWorkflowInput) -> str:
        if input.activity_input_data_size > 0:
            await workflow.execute_activity(
                large_payload_activity,
                LargePayloadActivityInput(data="i" * input.activity_input_data_size),
                schedule_to_close_timeout=timedelta(seconds=5),
            )
        return "o" * input.workflow_output_data_size


PAYLOAD_ERROR_LIMIT = 10 * 1024
PAYLOAD_LIMITS_EXTRA_ARGS = [
    "--dynamic-config-value",
    f"limit.blobSize.error={PAYLOAD_ERROR_LIMIT}",
    # Warn limit must be specified to have the server enforce the error limit
    "--dynamic-config-value",
    f"limit.blobSize.warn={2 * 1024}",
]


def _forwarding_runtime(logger: logging.Logger) -> Runtime:
    return Runtime(
        telemetry=TelemetryConfig(
            logging=LoggingConfig(
                filter=TelemetryFilter(core_level="WARN", other_level="ERROR"),
                forwarding=LogForwardingConfig(logger=logger),
            )
        )
    )


async def test_oversized_payload_fails_task_with_error_log(env: WorkflowEnvironment):
    """An oversized workflow completion is proactively failed by worker, which logs [TMPRL1103]."""
    if env.supports_time_skipping:
        pytest.skip("Time-skipping server does not report payload limits.")

    async with await WorkflowEnvironment.start_local(
        dev_server_extra_args=PAYLOAD_LIMITS_EXTRA_ARGS,
        dev_server_download_version=DEV_SERVER_DOWNLOAD_VERSION,
    ) as env:
        worker_logger = logging.getLogger(f"log-{uuid.uuid4()}")
        worker_client = await Client.connect(
            env.client.service_client.config.target_host,
            namespace=env.client.namespace,
            runtime=_forwarding_runtime(worker_logger),
        )

        def predicate(record: logging.LogRecord) -> bool:
            return (
                record.levelname == "ERROR"
                and "[TMPRL1103] Attempted to upload payloads with size that exceeded the error limit."
                in record.msg
            )

        with LogCapturer().logs_captured(worker_logger) as capturer:
            async with new_worker(
                worker_client, LargePayloadWorkflow, activities=[large_payload_activity]
            ) as worker:
                handle = await env.client.start_workflow(
                    LargePayloadWorkflow.run,
                    LargePayloadWorkflowInput(
                        activity_input_data_size=0,
                        workflow_output_data_size=PAYLOAD_ERROR_LIMIT + 1024,
                    ),
                    id=f"workflow-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                    execution_timeout=timedelta(seconds=3),
                )

                with pytest.raises(WorkflowFailureError) as err:
                    await handle.result()
                assert isinstance(err.value.cause, TimeoutError)
                assert err.value.cause.type == TimeoutType.START_TO_CLOSE

                # Core forwards logs on a buffered interval; poll while the capturer is attached.
                async def error_forwarded() -> None:
                    assert capturer.find(predicate) is not None

                await assert_eventually(error_forwarded)


async def test_disable_payload_error_limit_sends_to_server(env: WorkflowEnvironment):
    """With the opt-out, worker does not pre-fail; the oversized payload reaches (and is rejected by)
    the server."""
    if env.supports_time_skipping:
        pytest.skip("Time-skipping server does not report payload limits.")

    async with await WorkflowEnvironment.start_local(
        dev_server_extra_args=PAYLOAD_LIMITS_EXTRA_ARGS,
        dev_server_download_version=DEV_SERVER_DOWNLOAD_VERSION,
    ) as env:
        async with new_worker(
            env.client,
            LargePayloadWorkflow,
            activities=[large_payload_activity],
            disable_payload_error_limit=True,
        ) as worker:
            with pytest.raises(WorkflowFailureError) as err:
                await env.client.execute_workflow(
                    LargePayloadWorkflow.run,
                    LargePayloadWorkflowInput(
                        activity_input_data_size=PAYLOAD_ERROR_LIMIT + 1024,
                        workflow_output_data_size=0,
                    ),
                    id=f"workflow-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                    execution_timeout=timedelta(seconds=3),
                )

            assert isinstance(err.value.cause, TerminatedError)
            assert (
                err.value.cause.message
                == "BadScheduleActivityAttributes: ScheduleActivityTaskCommandAttributes.Input exceeds size limit."
            )


async def test_payload_size_warning_forwarded(env: WorkflowEnvironment):
    """The connection's warn threshold produces a forwarded [TMPRL1103] warning for over-threshold payloads."""
    worker_logger = logging.getLogger(f"log-{uuid.uuid4()}")
    worker_client = await Client.connect(
        env.client.service_client.config.target_host,
        namespace=env.client.namespace,
        runtime=_forwarding_runtime(worker_logger),
        data_converter=DataConverter(
            payload_limits=PayloadLimitsConfig(payload_size_warning=1024),
        ),
    )

    def predicate(record: logging.LogRecord) -> bool:
        return (
            record.levelname == "WARNING"
            and "[TMPRL1103] Attempted to upload payloads with size that exceeded the warning limit."
            in record.msg
        )

    with LogCapturer().logs_captured(worker_logger) as capturer:
        async with new_worker(
            worker_client, LargePayloadWorkflow, activities=[large_payload_activity]
        ) as worker:
            await worker_client.execute_workflow(
                LargePayloadWorkflow.run,
                LargePayloadWorkflowInput(
                    activity_input_data_size=0,
                    workflow_output_data_size=2 * 1024,
                ),
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=5),
            )

            # Core forwards logs on a buffered interval; poll while the capturer is attached.
            async def warning_forwarded() -> None:
                assert capturer.find(predicate) is not None

            await assert_eventually(warning_forwarded)
