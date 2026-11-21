"""Workflow run by both execution modes in the WASM benchmark."""

import asyncio
from datetime import timedelta

import temporalio.workflow as workflow

ACTIVITY = "wasm_bench_echo"
NEXUS_SERVICE = "WasmBenchmark"
MEMO_INTERVAL = 10
NEXUS_INTERVAL = 5
PAYLOAD = "x" * 1024
TIMER_INTERVAL = 25
TIMER_SECONDS = 1.0
TIMEOUT = timedelta(seconds=10)


@workflow.defn
class MixedHistoryWorkflow:
    def __init__(self) -> None:
        self.signals_received = 0

    @workflow.signal
    def advance(self, count: int) -> None:
        self.signals_received += count
        workflow.upsert_memo({"signals_received": self.signals_received})

    @workflow.run
    async def run(self, rounds: int, endpoint: str, signals: int = 0) -> int:
        nexus = workflow.create_nexus_client(endpoint=endpoint, service=NEXUS_SERVICE)
        total = 0
        for step in range(1, rounds + 1):
            if step % TIMER_INTERVAL == 0:
                await asyncio.sleep(TIMER_SECONDS)
            value = await workflow.execute_local_activity(
                ACTIVITY, PAYLOAD, result_type=str, start_to_close_timeout=TIMEOUT
            )
            value = await workflow.execute_activity(
                ACTIVITY, value, result_type=str, start_to_close_timeout=TIMEOUT
            )
            if step % NEXUS_INTERVAL == 0:
                value = await nexus.execute_operation(
                    "echo", value, output_type=str, schedule_to_close_timeout=TIMEOUT
                )
            if step % MEMO_INTERVAL == 0 or (signals and step == rounds):
                workflow.upsert_memo({"step": step})
            total += len(value)
        if signals:
            await workflow.wait_condition(lambda: self.signals_received == signals)
        return total + self.signals_received
