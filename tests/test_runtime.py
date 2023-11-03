import logging
import logging.handlers
import queue
import uuid
from typing import List, cast
from urllib.request import urlopen

from temporalio import workflow
from temporalio.client import Client
from temporalio.runtime import (
    LogForwardingConfig,
    LoggingConfig,
    PrometheusConfig,
    Runtime,
    TelemetryConfig,
    TelemetryFilter,
)
from temporalio.worker import Worker
from tests.helpers import assert_eq_eventually, find_free_port


@workflow.defn
class HelloWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return f"Hello, {name}!"


async def test_different_runtimes(client: Client):
    # Create two workers in separate runtimes and run workflows on them.
    # Confirm they each have different Prometheus addresses.
    prom_addr1 = f"127.0.0.1:{find_free_port()}"
    client1 = await Client.connect(
        client.service_client.config.target_host,
        namespace=client.namespace,
        runtime=Runtime(
            telemetry=TelemetryConfig(metrics=PrometheusConfig(bind_address=prom_addr1))
        ),
    )

    prom_addr2 = f"127.0.0.1:{find_free_port()}"
    client2 = await Client.connect(
        client.service_client.config.target_host,
        namespace=client.namespace,
        runtime=Runtime(
            telemetry=TelemetryConfig(metrics=PrometheusConfig(bind_address=prom_addr2))
        ),
    )

    async def run_workflow(client: Client):
        task_queue = f"task-queue-{uuid.uuid4()}"
        async with Worker(client, task_queue=task_queue, workflows=[HelloWorkflow]):
            assert "Hello, World!" == await client.execute_workflow(
                HelloWorkflow.run,
                "World",
                id=f"workflow-{uuid.uuid4()}",
                task_queue=task_queue,
            )

    await run_workflow(client1)
    await run_workflow(client2)

    # Get prom metrics on each
    with urlopen(url=f"http://{prom_addr1}/metrics") as f:
        assert "long_request" in f.read().decode("utf-8")
    with urlopen(url=f"http://{prom_addr2}/metrics") as f:
        assert "long_request" in f.read().decode("utf-8")


async def test_runtime_log_forwarding():
    # Create logger with record capture
    log_queue: queue.Queue[logging.LogRecord] = queue.Queue()
    log_queue_list = cast(List[logging.LogRecord], log_queue.queue)
    logger = logging.getLogger(f"log-{uuid.uuid4()}")
    logger.addHandler(logging.handlers.QueueHandler(log_queue))

    async def log_queue_len() -> int:
        return len(log_queue_list)

    # Create runtime
    runtime = Runtime(
        telemetry=TelemetryConfig(
            logging=LoggingConfig(
                filter=TelemetryFilter(core_level="DEBUG", other_level="ERROR"),
                forwarding=LogForwardingConfig(logger=logger),
            )
        )
    )

    # Set capture only info logs
    logger.setLevel(logging.INFO)
    # Write some logs
    runtime._core_runtime.write_test_info_log("info1", "extra1")
    runtime._core_runtime.write_test_debug_log("debug2", "extra2")
    runtime._core_runtime.write_test_info_log("info3", "extra3")

    # Check the expected records
    await assert_eq_eventually(2, log_queue_len)
    assert log_queue_list[0].levelno == logging.INFO
    assert log_queue_list[0].message.startswith(
        "[sdk_core::temporal_sdk_bridge::runtime] info1"
    )
    assert (
        log_queue_list[0].name
        == f"{logger.name}-sdk_core::temporal_sdk_bridge::runtime"
    )
    assert log_queue_list[0].created == log_queue_list[0].temporal_log.time  # type: ignore
    assert log_queue_list[0].temporal_log.fields == {"extra_data": "extra1"}  # type: ignore
    assert log_queue_list[1].levelno == logging.INFO
    assert log_queue_list[1].message.startswith(
        "[sdk_core::temporal_sdk_bridge::runtime] info3"
    )

    # Clear logs and enable debug and try again
    log_queue_list.clear()
    logger.setLevel(logging.DEBUG)
    runtime._core_runtime.write_test_info_log("info4", "extra4")
    runtime._core_runtime.write_test_debug_log("debug5", "extra5")
    runtime._core_runtime.write_test_info_log("info6", "extra6")
    await assert_eq_eventually(3, log_queue_len)
    assert log_queue_list[0].levelno == logging.INFO
    assert log_queue_list[0].message.startswith(
        "[sdk_core::temporal_sdk_bridge::runtime] info4"
    )
    assert log_queue_list[1].levelno == logging.DEBUG
    assert log_queue_list[1].message.startswith(
        "[sdk_core::temporal_sdk_bridge::runtime] debug5"
    )
    assert log_queue_list[2].levelno == logging.INFO
    assert log_queue_list[2].message.startswith(
        "[sdk_core::temporal_sdk_bridge::runtime] info6"
    )


@workflow.defn
class TaskFailWorkflow:
    @workflow.run
    async def run(self) -> None:
        raise RuntimeError("Intentional error")


async def test_runtime_task_fail_log_forwarding(client: Client):
    # Client with lo capturing runtime
    log_queue: queue.Queue[logging.LogRecord] = queue.Queue()
    log_queue_list = cast(List[logging.LogRecord], log_queue.queue)
    logger = logging.getLogger(f"log-{uuid.uuid4()}")
    logger.addHandler(logging.handlers.QueueHandler(log_queue))
    logger.setLevel(logging.WARN)
    client = await Client.connect(
        client.service_client.config.target_host,
        namespace=client.namespace,
        runtime=Runtime(
            telemetry=TelemetryConfig(
                logging=LoggingConfig(
                    filter=TelemetryFilter(core_level="WARN", other_level="ERROR"),
                    forwarding=LogForwardingConfig(logger=logger),
                )
            )
        ),
    )

    # Start workflow
    task_queue = f"task-queue-{uuid.uuid4()}"
    async with Worker(client, task_queue=task_queue, workflows=[TaskFailWorkflow]):
        handle = await client.start_workflow(
            TaskFailWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=task_queue,
        )

        # Wait for log to appear
        async def has_log() -> bool:
            return any(
                l for l in log_queue_list if "Failing workflow task" in l.message
            )

        await assert_eq_eventually(True, has_log)

    # Check record
    record = next((l for l in log_queue_list if "Failing workflow task" in l.message))
    assert record.levelno == logging.WARNING
    assert record.name == f"{logger.name}-sdk_core::temporal_sdk_core::worker::workflow"
    assert record.temporal_log.fields["run_id"] == handle.result_run_id  # type: ignore
