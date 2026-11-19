import asyncio
import os
import time
from datetime import timedelta

import temporalio.workflow as workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from .helper import suffix

calls = 0

assert not os.environ


@workflow.defn
class WasmRoundTrip:
    def __init__(self) -> None:
        global calls
        calls += 1
        self.signalled: bool = False

    @workflow.run
    async def run(self, endpoint: str) -> str:
        assert endpoint == f"nexus-endpoint-{workflow.info().task_queue}"
        await asyncio.sleep(0.01)
        local: str = await workflow.execute_local_activity(
            "wasm_local",
            "guest",
            result_type=str,
            start_to_close_timeout=timedelta(seconds=5),
            local_retry_threshold=timedelta(milliseconds=10),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(milliseconds=50), maximum_attempts=2
            ),
        )
        remote: str = await workflow.execute_activity(
            "wasm_remote",
            local,
            result_type=str,
            start_to_close_timeout=timedelta(seconds=5),
        )
        nexus = workflow.create_nexus_client(endpoint=endpoint, service="WasmNexus")
        result = await nexus.execute_operation(
            "echo",
            remote,
            output_type=str,
            schedule_to_close_timeout=timedelta(seconds=5),
        )
        await workflow.wait_condition(lambda: self.signalled)
        return f"{result}{suffix()}:{calls}"

    @workflow.signal
    def finish(self) -> None:
        self.signalled = True

    @workflow.query
    def state(self) -> int:
        return calls


@workflow.defn
class WasmProbe:
    @workflow.run
    async def run(self, probe: str) -> str:
        if probe == "clock":
            time.time()
        elif probe == "entropy":
            os.urandom(1)
        elif probe == "file":
            with open("/dev/null"):
                pass
        elif probe == "fuel":
            while True:
                pass
        elif probe == "memory":
            bytearray(200_000_000)
        elif probe == "invalid-completion":
            from temporalio.bridge.proto.workflow_completion import (
                WorkflowActivationCompletion,
            )

            setattr(
                WorkflowActivationCompletion, "SerializeToString", lambda self: b"\xff"
            )
        elif probe == "application-failure":
            raise ApplicationError("failed in guest")
        elif probe == "local-activity":
            try:
                await workflow.execute_local_activity(
                    "missing", start_to_close_timeout=timedelta(seconds=5)
                )
            except ValueError as exc:
                return str(exc)
        return "ok"
