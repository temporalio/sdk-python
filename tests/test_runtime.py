import logging
import logging.handlers
import queue
import re
import uuid
from datetime import timedelta
from typing import List, cast
from urllib.request import urlopen

import pytest

from temporalio import workflow
from temporalio.client import Client
from temporalio.runtime import (
    LogForwardingConfig,
    LoggingConfig,
    PrometheusConfig,
    Runtime,
    TelemetryConfig,
    TelemetryFilter,
    _RuntimeRef,
)
from temporalio.worker import Worker
from tests.helpers import assert_eq_eventually, assert_eventually, find_free_port


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


async def test_prometheus_histogram_bucket_overrides(client: Client):
    # Set up a Prometheus configuration with custom histogram bucket overrides
    prom_addr = f"127.0.0.1:{find_free_port()}"
    special_value = float(1234.5678)
    histogram_overrides = {
        "temporal_long_request_latency": [special_value / 2, special_value],
        "custom_histogram": [special_value / 2, special_value],
    }

    runtime = Runtime(
        telemetry=TelemetryConfig(
            metrics=PrometheusConfig(
                bind_address=prom_addr,
                counters_total_suffix=False,
                unit_suffix=False,
                durations_as_seconds=False,
                histogram_bucket_overrides=histogram_overrides,
            ),
        ),
    )

    # Create a custom histogram metric
    custom_histogram = runtime.metric_meter.create_histogram(
        "custom_histogram", "Custom histogram", "ms"
    )

    # Record a value to the custom histogram
    custom_histogram.record(600)

    # Create client with overrides
    client_with_overrides = await Client.connect(
        client.service_client.config.target_host,
        namespace=client.namespace,
        runtime=runtime,
    )

    async def run_workflow(client: Client):
        task_queue = f"task-queue-{uuid.uuid4()}"
        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[HelloWorkflow],
        ):
            assert "Hello, World!" == await client.execute_workflow(
                HelloWorkflow.run,
                "World",
                id=f"workflow-{uuid.uuid4()}",
                task_queue=task_queue,
            )

    await run_workflow(client_with_overrides)

    async def check_metrics() -> None:
        with urlopen(url=f"http://{prom_addr}/metrics") as f:
            metrics_output = f.read().decode("utf-8")

            for key, buckets in histogram_overrides.items():
                assert (
                    key in metrics_output
                ), f"Missing {key} in full output: {metrics_output}"
                for bucket in buckets:
                    # expect to have {key}_bucket and le={bucket} in the same line with arbitrary strings between them
                    regex = re.compile(f'{key}_bucket.*le="{bucket}"')
                    assert regex.search(
                        metrics_output
                    ), f"Missing bucket for {key} in full output: {metrics_output}"

    # Wait for metrics to appear and match the expected buckets
    await assert_eventually(check_metrics)


def test_runtime_ref_creates_default():
    ref = _RuntimeRef()
    assert not ref._default_runtime
    ref.default()
    assert ref._default_runtime


def test_runtime_ref_prevents_default():
    ref = _RuntimeRef()
    ref.prevent_default()
    with pytest.raises(RuntimeError) as exc_info:
        ref.default()
    assert exc_info.match(
        "Cannot create default Runtime after Runtime.prevent_default has been called"
    )

    # explicitly setting a default runtime will allow future calls to `default()``
    explicit_runtime = Runtime(telemetry=TelemetryConfig())
    ref.set_default(explicit_runtime)

    assert ref.default() is explicit_runtime


def test_runtime_ref_prevent_default_errors_after_default():
    ref = _RuntimeRef()
    ref.default()
    with pytest.raises(RuntimeError) as exc_info:
        ref.prevent_default()

    assert exc_info.match(
        "Runtime.prevent_default called after default runtime has been created"
    )


def test_runtime_ref_set_default():
    ref = _RuntimeRef()
    explicit_runtime = Runtime(telemetry=TelemetryConfig())
    ref.set_default(explicit_runtime)
    assert ref.default() is explicit_runtime

    new_runtime = Runtime(telemetry=TelemetryConfig())

    with pytest.raises(RuntimeError) as exc_info:
        ref.set_default(new_runtime)
    assert exc_info.match("Runtime default already set")

    ref.set_default(new_runtime, error_if_already_set=False)
    assert ref.default() is new_runtime
