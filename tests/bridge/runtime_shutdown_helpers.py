"""Native callbacks for the interpreter shutdown subprocess."""

import asyncio
import threading
from collections.abc import Callable, Sequence
from contextvars import Context
from typing import Any
from unittest.mock import patch

import temporalio.api.workflowservice.v1
import temporalio.bridge.runtime
import temporalio.client
import temporalio.service


def start_runtime(
    release: threading.Event, completed: list[bool]
) -> temporalio.bridge.runtime.Runtime:
    started = threading.Event()

    def forward_logs(
        _entries: Sequence[temporalio.bridge.runtime.BufferedLogEntry],
    ) -> None:
        started.set()
        assert release.wait(10)
        completed.append(True)

    runtime = temporalio.bridge.runtime.Runtime(
        options=temporalio.bridge.runtime.RuntimeOptions(
            telemetry=temporalio.bridge.runtime.TelemetryConfig(
                logging=temporalio.bridge.runtime.LoggingConfig(
                    filter="INFO", forward_to=forward_logs, format=None
                ),
                metrics=None,
            )
        )
    )
    runtime.write_test_info_log("before interpreter shutdown", "")
    assert started.wait(10)
    return runtime


async def run_client(
    connect_config: temporalio.service.ConnectConfig,
    namespace: str,
    release: threading.Event,
    completed: list[bool],
) -> None:
    client = await temporalio.client.Client.connect(
        namespace=namespace, **vars(connect_config)
    )
    started = threading.Event()
    loop = asyncio.get_running_loop()
    original_call_soon = loop.call_soon_threadsafe

    def call_soon(
        callback: Callable[..., Any], *args: Any, context: Context | None = None
    ) -> asyncio.Handle:
        result = original_call_soon(callback, *args, context=context)
        started.set()
        assert release.wait(10)
        completed.append(True)
        return result

    # Let Python receive the RPC result while the Rust completion callback
    # is still active, reproducing the interpreter shutdown race.
    with patch.object(loop, "call_soon_threadsafe", call_soon):
        await client.workflow_service.describe_namespace(
            temporalio.api.workflowservice.v1.DescribeNamespaceRequest(
                namespace=client.namespace
            )
        )
        assert started.wait(10)
