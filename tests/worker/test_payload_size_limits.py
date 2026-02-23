import dataclasses
import logging
import uuid
import warnings
from dataclasses import dataclass
from datetime import timedelta

import pytest

import temporalio
import temporalio.converter
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.converter import PayloadLimitsConfig, PayloadSizeWarning
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
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
from temporalio.worker._replayer import Replayer
from tests import DEV_SERVER_DOWNLOAD_VERSION
from tests.helpers import LogCapturer, new_worker


@dataclass
class LargePayloadWorkflowInput:
    activity_input_data_size: int
    activity_output_data_size: int
    activity_exception_data_size: int
    workflow_output_data_size: int
    data: str


@dataclass
class LargePayloadWorkflowOutput:
    data: str


@dataclass
class LargePayloadActivityInput:
    exception_data_size: int
    output_data_size: int
    data: str


@dataclass
class LargePayloadActivityOutput:
    data: str


@activity.defn
async def large_payload_activity(
    input: LargePayloadActivityInput,
) -> LargePayloadActivityOutput:
    if input.exception_data_size > 0:
        raise ApplicationError(
            "Intentional activity failure", "e" * input.exception_data_size
        )
    return LargePayloadActivityOutput(data="o" * input.output_data_size)


@workflow.defn
class LargePayloadWorkflow:
    @workflow.run
    async def run(self, input: LargePayloadWorkflowInput) -> LargePayloadWorkflowOutput:
        await workflow.execute_activity(
            large_payload_activity,
            LargePayloadActivityInput(
                exception_data_size=input.activity_exception_data_size,
                output_data_size=input.activity_output_data_size,
                data="i" * input.activity_input_data_size,
            ),
            schedule_to_close_timeout=timedelta(seconds=5),
        )
        return LargePayloadWorkflowOutput(data="o" * input.workflow_output_data_size)


PAYLOAD_ERROR_LIMIT = 10 * 1024
PAYLOAD_LIMITS_EXTRA_ARGS = [
    "--dynamic-config-value",
    f"limit.blobSize.error={PAYLOAD_ERROR_LIMIT}",
    # Warn limit must be specified to have the server enforce the error limit
    "--dynamic-config-value",
    f"limit.blobSize.warn={2 * 1024}",
]


async def test_payload_size_warning_workflow_input(client: Client):
    config = client.config()
    config["data_converter"] = dataclasses.replace(
        temporalio.converter.default(),
        payload_limits=PayloadLimitsConfig(
            payload_size_warning=100,
        ),
    )
    client = Client(**config)

    with warnings.catch_warnings(record=True) as w:
        async with new_worker(
            client, LargePayloadWorkflow, activities=[large_payload_activity]
        ) as worker:
            await client.execute_workflow(
                LargePayloadWorkflow.run,
                LargePayloadWorkflowInput(
                    activity_input_data_size=0,
                    activity_output_data_size=0,
                    activity_exception_data_size=0,
                    workflow_output_data_size=0,
                    data="i" * 2 * 1024,
                ),
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

        assert len(w) == 1
        assert issubclass(w[-1].category, PayloadSizeWarning)
        assert (
            "[TMPRL1103] Attempted to upload payloads with size that exceeded the warning limit."
            in str(w[-1].message)
        )


async def test_payload_size_warning_workflow_memo(client: Client):
    config = client.config()
    config["data_converter"] = dataclasses.replace(
        temporalio.converter.default(),
        payload_limits=PayloadLimitsConfig(memo_size_warning=128),
    )
    client = Client(**config)

    with warnings.catch_warnings(record=True) as w:
        async with new_worker(
            client, LargePayloadWorkflow, activities=[large_payload_activity]
        ) as worker:
            await client.execute_workflow(
                LargePayloadWorkflow.run,
                LargePayloadWorkflowInput(
                    activity_input_data_size=0,
                    activity_output_data_size=0,
                    activity_exception_data_size=0,
                    workflow_output_data_size=0,
                    data="",
                ),
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                memo={
                    "key1": [0] * 64,
                    "key2": [0] * 64,
                    "key3": [0] * 64,
                },
            )

        assert len(w) == 1
        assert issubclass(w[-1].category, PayloadSizeWarning)
        assert (
            "[TMPRL1103] Attempted to upload memo with size that exceeded the warning limit."
            in str(w[-1].message)
        )


async def test_payload_size_error_disabled_workflow_payload(env: WorkflowEnvironment):
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
                        activity_output_data_size=0,
                        activity_exception_data_size=0,
                        workflow_output_data_size=0,
                        data="",
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


async def test_payload_size_error_workflow_result(env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip("Time-skipping server does not report payload limits.")

    async with await WorkflowEnvironment.start_local(
        dev_server_extra_args=PAYLOAD_LIMITS_EXTRA_ARGS,
        dev_server_download_version=DEV_SERVER_DOWNLOAD_VERSION,
    ) as env:
        # Create worker runtime with forwarded logger
        worker_logger = logging.getLogger(f"log-{uuid.uuid4()}")
        worker_runtime = Runtime(
            telemetry=TelemetryConfig(
                logging=LoggingConfig(
                    filter=TelemetryFilter(core_level="WARN", other_level="ERROR"),
                    forwarding=LogForwardingConfig(logger=worker_logger),
                )
            )
        )

        # Create client for worker with custom runtime logging
        worker_client = await Client.connect(
            env.client.service_client.config.target_host,
            namespace=env.client.namespace,
            runtime=worker_runtime,
        )

        with (
            LogCapturer().logs_captured(worker_logger) as worker_logger_capturer,
            LogCapturer().logs_captured(logging.getLogger()) as root_logger_capturer,
        ):
            async with new_worker(
                worker_client, LargePayloadWorkflow, activities=[large_payload_activity]
            ) as worker:
                handle = await env.client.start_workflow(
                    LargePayloadWorkflow.run,
                    LargePayloadWorkflowInput(
                        activity_input_data_size=0,
                        activity_output_data_size=0,
                        activity_exception_data_size=0,
                        workflow_output_data_size=PAYLOAD_ERROR_LIMIT + 1024,
                        data="",
                    ),
                    id=f"workflow-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                    execution_timeout=timedelta(seconds=3),
                )

                with pytest.raises(WorkflowFailureError) as err:
                    await handle.result()

                assert isinstance(err.value.cause, TimeoutError)
                assert err.value.cause.type == TimeoutType.START_TO_CLOSE

                replayer = Replayer(workflows=[LargePayloadWorkflow])
                await replayer.replay_workflow(await handle.fetch_history())

            def worker_logger_predicate(record: logging.LogRecord) -> bool:
                return (
                    record.levelname == "WARNING"
                    and "[TMPRL1103] Attempted to upload payloads with size that exceeded the error limit."
                    in record.msg
                )

            assert worker_logger_capturer.find(worker_logger_predicate)

            def root_logger_predicate(record: logging.LogRecord) -> bool:
                return (
                    record.levelname == "WARNING"
                    and "[TMPRL1103] Attempted to upload payloads with size that exceeded the error limit."
                    in record.msg
                )

            assert root_logger_capturer.find(root_logger_predicate)


async def test_payload_size_warning_workflow_result(client: Client):
    config = client.config()
    config["data_converter"] = dataclasses.replace(
        temporalio.converter.default(),
        payload_limits=PayloadLimitsConfig(
            payload_size_warning=1024,
        ),
    )
    worker_client = Client(**config)

    with warnings.catch_warnings(record=True) as w:
        async with new_worker(
            worker_client, LargePayloadWorkflow, activities=[large_payload_activity]
        ) as worker:
            await client.execute_workflow(
                LargePayloadWorkflow.run,
                LargePayloadWorkflowInput(
                    activity_input_data_size=0,
                    activity_output_data_size=0,
                    activity_exception_data_size=0,
                    workflow_output_data_size=2 * 1024,
                    data="",
                ),
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=3),
            )

        assert len(w) == 1
        assert issubclass(w[-1].category, PayloadSizeWarning)
        assert (
            "[TMPRL1103] Attempted to upload payloads with size that exceeded the warning limit."
            in str(w[-1].message)
        )


async def test_payload_size_error_activity_input(env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip("Time-skipping server does not report payload limits.")

    async with await WorkflowEnvironment.start_local(
        dev_server_extra_args=PAYLOAD_LIMITS_EXTRA_ARGS,
        dev_server_download_version=DEV_SERVER_DOWNLOAD_VERSION,
    ) as env:
        # Create worker runtime with forwarded logger
        worker_logger = logging.getLogger(f"log-{uuid.uuid4()}")
        worker_runtime = Runtime(
            telemetry=TelemetryConfig(
                logging=LoggingConfig(
                    filter=TelemetryFilter(core_level="WARN", other_level="ERROR"),
                    forwarding=LogForwardingConfig(logger=worker_logger),
                )
            )
        )

        # Create client for worker with custom runtime logging
        worker_client = await Client.connect(
            env.client.service_client.config.target_host,
            namespace=env.client.namespace,
            runtime=worker_runtime,
        )

        with (
            LogCapturer().logs_captured(worker_logger) as worker_logger_capturer,
            LogCapturer().logs_captured(logging.getLogger()) as root_logger_capturer,
        ):
            async with new_worker(
                worker_client, LargePayloadWorkflow, activities=[large_payload_activity]
            ) as worker:
                handle = await env.client.start_workflow(
                    LargePayloadWorkflow.run,
                    LargePayloadWorkflowInput(
                        activity_input_data_size=PAYLOAD_ERROR_LIMIT + 1024,
                        activity_output_data_size=0,
                        activity_exception_data_size=0,
                        workflow_output_data_size=0,
                        data="",
                    ),
                    id=f"workflow-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                    execution_timeout=timedelta(seconds=3),
                )

                with pytest.raises(WorkflowFailureError) as err:
                    await handle.result()

                assert isinstance(err.value.cause, TimeoutError)

                replayer = Replayer(workflows=[LargePayloadWorkflow])
                await replayer.replay_workflow(await handle.fetch_history())

            def worker_logger_predicate(record: logging.LogRecord) -> bool:
                return (
                    record.levelname == "WARNING"
                    and "[TMPRL1103] Attempted to upload payloads with size that exceeded the error limit."
                    in record.msg
                )

            assert worker_logger_capturer.find(worker_logger_predicate)

            def root_logger_predicate(record: logging.LogRecord) -> bool:
                return (
                    record.levelname == "WARNING"
                    and "[TMPRL1103] Attempted to upload payloads with size that exceeded the error limit."
                    in record.msg
                )

            assert root_logger_capturer.find(root_logger_predicate)


async def test_payload_size_warning_activity_input(client: Client):
    config = client.config()
    config["data_converter"] = dataclasses.replace(
        temporalio.converter.default(),
        payload_limits=PayloadLimitsConfig(
            payload_size_warning=1024,
        ),
    )
    worker_client = Client(**config)

    with warnings.catch_warnings(record=True) as w:
        async with new_worker(
            worker_client, LargePayloadWorkflow, activities=[large_payload_activity]
        ) as worker:
            await client.execute_workflow(
                LargePayloadWorkflow.run,
                LargePayloadWorkflowInput(
                    activity_input_data_size=2 * 1024,
                    activity_output_data_size=0,
                    activity_exception_data_size=0,
                    workflow_output_data_size=0,
                    data="",
                ),
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

        assert len(w) == 1
        assert issubclass(w[-1].category, PayloadSizeWarning)
        assert (
            "[TMPRL1103] Attempted to upload payloads with size that exceeded the warning limit."
            in str(w[-1].message)
        )


async def test_payload_size_error_activity_exception(env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip("Time-skipping server does not report payload limits.")

    async with await WorkflowEnvironment.start_local(
        dev_server_extra_args=PAYLOAD_LIMITS_EXTRA_ARGS,
        dev_server_download_version=DEV_SERVER_DOWNLOAD_VERSION,
    ) as env:
        # Create worker runtime with forwarded logger
        worker_logger = logging.getLogger(f"log-{uuid.uuid4()}")
        worker_runtime = Runtime(
            telemetry=TelemetryConfig(
                logging=LoggingConfig(
                    filter=TelemetryFilter(core_level="WARN", other_level="ERROR"),
                    forwarding=LogForwardingConfig(logger=worker_logger),
                )
            )
        )

        # Create client for worker with custom runtime logging
        worker_client = await Client.connect(
            env.client.service_client.config.target_host,
            namespace=env.client.namespace,
            runtime=worker_runtime,
        )

        with (
            LogCapturer().logs_captured(
                activity.logger.base_logger
            ) as activity_logger_capturer,
        ):
            async with new_worker(
                worker_client, LargePayloadWorkflow, activities=[large_payload_activity]
            ) as worker:
                handle = await env.client.start_workflow(
                    LargePayloadWorkflow.run,
                    LargePayloadWorkflowInput(
                        activity_input_data_size=0,
                        activity_output_data_size=0,
                        activity_exception_data_size=PAYLOAD_ERROR_LIMIT + 1024,
                        workflow_output_data_size=0,
                        data="",
                    ),
                    id=f"workflow-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                )

                with pytest.raises(WorkflowFailureError) as err:
                    await handle.result()

                assert isinstance(err.value.cause, ActivityError)
                assert isinstance(err.value.cause.cause, ApplicationError)

                replayer = Replayer(workflows=[LargePayloadWorkflow])
                await replayer.replay_workflow(await handle.fetch_history())

            def activity_logger_predicate(record: logging.LogRecord) -> bool:
                return (
                    record.levelname == "ERROR"
                    and "[TMPRL1103] Attempted to upload payloads with size that exceeded the error limit."
                    in record.msg
                )

            assert activity_logger_capturer.find(activity_logger_predicate)


async def test_payload_size_error_activity_result(env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip("Time-skipping server does not report payload limits.")

    async with await WorkflowEnvironment.start_local(
        dev_server_extra_args=PAYLOAD_LIMITS_EXTRA_ARGS,
        dev_server_download_version=DEV_SERVER_DOWNLOAD_VERSION,
    ) as env:
        # Create worker runtime with forwarded logger
        worker_logger = logging.getLogger(f"log-{uuid.uuid4()}")
        worker_runtime = Runtime(
            telemetry=TelemetryConfig(
                logging=LoggingConfig(
                    filter=TelemetryFilter(core_level="WARN", other_level="ERROR"),
                    forwarding=LogForwardingConfig(logger=worker_logger),
                )
            )
        )

        # Create client for worker with custom runtime logging
        worker_client = await Client.connect(
            env.client.service_client.config.target_host,
            namespace=env.client.namespace,
            runtime=worker_runtime,
        )

        with (
            LogCapturer().logs_captured(
                activity.logger.base_logger
            ) as activity_logger_capturer,
        ):
            async with new_worker(
                worker_client, LargePayloadWorkflow, activities=[large_payload_activity]
            ) as worker:
                handle = await env.client.start_workflow(
                    LargePayloadWorkflow.run,
                    LargePayloadWorkflowInput(
                        activity_input_data_size=0,
                        activity_output_data_size=PAYLOAD_ERROR_LIMIT + 1024,
                        activity_exception_data_size=0,
                        workflow_output_data_size=0,
                        data="",
                    ),
                    id=f"workflow-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                )

                with pytest.raises(WorkflowFailureError) as err:
                    await handle.result()

                assert isinstance(err.value.cause, ActivityError)
                assert isinstance(err.value.cause.cause, ApplicationError)

                assert handle is not None
                replayer = Replayer(workflows=[LargePayloadWorkflow])
                await replayer.replay_workflow(await handle.fetch_history())

            def activity_logger_predicate(record: logging.LogRecord) -> bool:
                return (
                    hasattr(record, "__temporal_error_identifier")
                    and getattr(record, "__temporal_error_identifier")
                    == "PayloadSizeError"
                    and record.levelname == "WARNING"
                    and "[TMPRL1103] Attempted to upload payloads with size that exceeded the error limit."
                    in record.msg
                )

            assert activity_logger_capturer.find(activity_logger_predicate)


async def test_payload_size_warning_activity_result(client: Client):
    config = client.config()
    config["data_converter"] = dataclasses.replace(
        temporalio.converter.default(),
        payload_limits=PayloadLimitsConfig(
            payload_size_warning=1024,
        ),
    )
    worker_client = Client(**config)

    with warnings.catch_warnings(record=True) as w:
        async with new_worker(
            worker_client, LargePayloadWorkflow, activities=[large_payload_activity]
        ) as worker:
            await client.execute_workflow(
                LargePayloadWorkflow.run,
                LargePayloadWorkflowInput(
                    activity_input_data_size=0,
                    activity_output_data_size=2 * 1024,
                    activity_exception_data_size=0,
                    workflow_output_data_size=0,
                    data="",
                ),
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

        assert len(w) == 1
        assert issubclass(w[-1].category, PayloadSizeWarning)
        assert (
            "[TMPRL1103] Attempted to upload payloads with size that exceeded the warning limit."
            in str(w[-1].message)
        )
