"""Measure workflow activation and wall time with native Python and a WASM bundle.

Build the bundle with scripts/build_workflow_wasm.py using --source scripts/wasm,
--workflow bench_workflow:MixedHistoryWorkflow, and staged guest dependencies.
Run with python -m scripts.bench_workflow_wasm --bundle <path> --output <path>.
Use --rounds 200 --signals 1667 for a 10,000-event history.

Activation time covers instance creation and synchronous execution in the SDK.
Live wall time runs from workflow start through result. Replay wall time covers
submitting a recorded history through replay completion. Worker setup is timed
separately and both replayers use the same WASM histories.

The native runner dispatches cache eviction through the activation method. WASM
eviction drops the store separately, so it is outside the activation timings.
"""

import argparse
import asyncio
import json
import math
import platform
import statistics
import sys
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

from nexusrpc.handler import StartOperationContext, service_handler, sync_operation

import temporalio.activity as activity
from temporalio.api.enums.v1 import EventType
from temporalio.bridge.proto.workflow_activation import (
    InitializeWorkflow,
    WorkflowActivation,
)
from temporalio.bridge.proto.workflow_completion import WorkflowActivationCompletion
from temporalio.client import Client, WorkflowHandle, WorkflowHistory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from temporalio.worker._workflow import _RunningWorkflow, _WorkflowWorker
from temporalio.worker._workflow_instance import WorkflowInstance

from .wasm.bench_workflow import (
    ACTIVITY,
    MEMO_INTERVAL,
    NEXUS_INTERVAL,
    NEXUS_SERVICE,
    PAYLOAD,
    TIMER_INTERVAL,
    TIMER_SECONDS,
    MixedHistoryWorkflow,
)


@activity.defn(name=ACTIVITY)
async def echo(value: str) -> str:
    return value


@service_handler(name=NEXUS_SERVICE)
class Nexus:
    @sync_operation
    async def echo(self, context: StartOperationContext, input: str) -> str:
        return input


@dataclass
class ActivationTiming:
    seconds: float
    jobs: tuple[str, ...]
    commands: tuple[str, ...]
    replaying: bool


class ActivationRecorder:
    def __init__(self) -> None:
        self.samples: list[ActivationTiming] = []
        self._creation_seconds: dict[str, float] = {}

    def take(self) -> list[ActivationTiming]:
        samples, self.samples = self.samples, []
        return samples

    def create_instance(
        self,
        worker: _WorkflowWorker,
        act: WorkflowActivation,
        init: InitializeWorkflow,
    ) -> WorkflowInstance:
        started = perf_counter()
        instance = _create_instance(worker, act, init)
        self._creation_seconds[act.run_id] = perf_counter() - started
        return instance

    def activate(
        self, workflow: _RunningWorkflow, act: WorkflowActivation
    ) -> WorkflowActivationCompletion:
        started = perf_counter()
        completion = _activate(workflow, act)
        seconds = perf_counter() - started + self._creation_seconds.pop(act.run_id, 0)
        jobs = []
        for job in act.jobs:
            name: str = job.WhichOneof("variant") or "unknown"
            if name == "resolve_activity":
                name = (
                    "resolve_local_activity"
                    if job.resolve_activity.is_local
                    else "resolve_remote_activity"
                )
            jobs.append(name)
        self.samples.append(
            ActivationTiming(
                seconds,
                tuple(jobs),
                tuple(
                    command.WhichOneof("variant") or "unknown"
                    for command in completion.successful.commands
                ),
                act.is_replaying,
            )
        )
        return completion


_create_instance = _WorkflowWorker._create_workflow_instance
_activate = _RunningWorkflow.activate


def distribution(seconds: Sequence[float]) -> dict[str, float | int]:
    ordered = sorted(seconds)
    if not ordered:
        return {"count": 0}
    return {
        "count": len(ordered),
        "total_ms": sum(ordered) * 1000,
        "mean_ms": statistics.mean(ordered) * 1000,
        "median_ms": statistics.median(ordered) * 1000,
        "p95_ms": ordered[math.ceil(len(ordered) * 0.95) - 1] * 1000,
        "p99_ms": ordered[math.ceil(len(ordered) * 0.99) - 1] * 1000,
        "max_ms": ordered[-1] * 1000,
    }


def summarize(
    setup_seconds: float, runs: list[tuple[float, list[ActivationTiming]]]
) -> dict[str, object]:
    groups: dict[str, list[float]] = {}
    command_counts: Counter[str] = Counter()
    initial: list[float] = []
    steady: list[float] = []
    for _, activations in runs:
        for act in activations:
            groups.setdefault(" + ".join(sorted(act.jobs)), []).append(act.seconds)
            command_counts.update(act.commands)
            (initial if "initialize_workflow" in act.jobs else steady).append(
                act.seconds
            )
    return {
        "worker_setup_ms": setup_seconds * 1000,
        "wall_ms": distribution([seconds for seconds, _ in runs]),
        "activation_ms": distribution(initial + steady),
        "initial_activation_ms": distribution(initial),
        "steady_activation_ms": distribution(steady),
        "activation_ms_by_jobs": {
            jobs: distribution(values) for jobs, values in sorted(groups.items())
        },
        "command_counts": dict(sorted(command_counts.items())),
        "runs": [
            {
                "wall_ms": seconds * 1000,
                "activation_count": len(activations),
                "replaying_activation_count": sum(a.replaying for a in activations),
                "activation_total_ms": sum(a.seconds for a in activations) * 1000,
            }
            for seconds, activations in runs
        ],
    }


async def wait_for_memo(
    handle: WorkflowHandle[object, int], key: str, value: int, *, poll_interval: float
) -> None:
    while True:
        description = await handle.describe()
        if await description.memo_value(key, 0, type_hint=int) == value:
            return
        await asyncio.sleep(poll_interval)


async def live(
    env: WorkflowEnvironment,
    client: Client,
    bundle: Path | None,
    rounds: int,
    runs: int,
    recorder: ActivationRecorder,
    *,
    signals: int,
) -> tuple[list[WorkflowHistory], dict[str, object]]:
    queue = f"wasm-bench-{uuid.uuid4()}"
    endpoint = f"endpoint-{queue}"
    await env.create_nexus_endpoint(endpoint, queue)
    histories = []
    samples = []
    signal_times = []
    setup_started = perf_counter()
    async with Worker(
        client,
        task_queue=queue,
        workflows=[] if bundle else [MixedHistoryWorkflow],
        wasm_workflow_bundle=bundle,
        activities=[echo],
        nexus_service_handlers=[Nexus()],
        max_cached_workflows=10,
    ):
        setup = perf_counter() - setup_started
        for _ in range(runs + 1):
            recorder.take()
            started = perf_counter()
            signal_started: float | None = None
            async with asyncio.timeout(180):
                handle = await client.start_workflow(
                    "MixedHistoryWorkflow",
                    args=[rounds, endpoint, signals] if signals else [rounds, endpoint],
                    result_type=int,
                    id=f"wasm-bench-{uuid.uuid4()}",
                    task_queue=queue,
                )
                if signals:
                    await wait_for_memo(handle, "step", rounds, poll_interval=0.05)
                    signal_started = perf_counter()
                    for sent in range(1, signals + 1):
                        await handle.signal("advance", 1)
                        if sent < signals:
                            # A committed memo guarantees the next signal has its own Workflow Task.
                            await wait_for_memo(
                                handle, "signals_received", sent, poll_interval=0.002
                            )
                result = await handle.result()
            completed = perf_counter()
            if signal_started is not None:
                signal_times.append(completed - signal_started)
            if result != rounds * len(PAYLOAD) + signals:
                raise RuntimeError(f"Unexpected workflow result {result}")
            histories.append(await handle.fetch_history())
            samples.append((completed - started, recorder.take()))
    summary = summarize(setup, samples[1:])
    if signals:
        summary["signal_phase_wall_ms"] = distribution(signal_times[1:])
    summary["history_event_counts"] = [len(history.events) for history in histories[1:]]
    summary["history_event_types"] = dict(
        sorted(
            Counter(EventType.Name(e.event_type) for e in histories[1].events).items()
        )
    )
    return histories, summary


async def replay(
    histories: list[WorkflowHistory],
    bundle: Path | None,
    namespace: str,
    recorder: ActivationRecorder,
) -> dict[str, object]:
    async def input_histories() -> AsyncIterator[WorkflowHistory]:
        for history in histories:
            yield history

    samples = []
    setup_started = perf_counter()
    async with Replayer(
        workflows=[] if bundle else [MixedHistoryWorkflow],
        wasm_workflow_bundle=bundle,
        namespace=namespace,
    ).workflow_replay_iterator(input_histories()) as iterator:
        setup = perf_counter() - setup_started
        for _ in histories:
            recorder.take()
            started = perf_counter()
            async with asyncio.timeout(180):
                result = await anext(iterator)
            elapsed = perf_counter() - started
            if result.replay_failure:
                raise result.replay_failure
            samples.append((elapsed, recorder.take()))
    return summarize(setup, samples[1:])


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--dev-server", help="Path to a local Temporal CLI binary")
    parser.add_argument("--rounds", type=int, default=200)
    parser.add_argument("--runs", type=int, default=5, help="Measured runs per mode")
    parser.add_argument("--signals", type=int, default=0, help="Signals per workflow")
    parser.add_argument("--output", type=Path, help="Save the JSON results to a file")
    args = parser.parse_args()
    if min(args.rounds, args.runs) < 1:
        parser.error("--rounds and --runs must be positive")
    if args.signals < 0:
        parser.error("--signals must be nonnegative")

    recorder = ActivationRecorder()
    output: dict[str, object] = {
        "host_python": platform.python_version(),
        "rounds": args.rounds,
        "runs": args.runs,
        "signals": args.signals,
        "warmup_runs_per_phase": 1,
        "timer_ms": TIMER_SECONDS * 1000,
        "timer_every_rounds": TIMER_INTERVAL,
        "payload_bytes": len(PAYLOAD),
        "nexus_every_rounds": NEXUS_INTERVAL,
        "memo_every_rounds": MEMO_INTERVAL,
        "bundle_bytes": args.bundle.stat().st_size,
        "replay_history_source": "wasm_live",
    }
    with (
        patch.object(
            _WorkflowWorker,
            "_create_workflow_instance",
            lambda worker, act, init: recorder.create_instance(worker, act, init),
        ),
        patch.object(
            _RunningWorkflow, "activate", lambda wf, act: recorder.activate(wf, act)
        ),
    ):
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=args.dev_server
        ) as env:
            config = env.client.config()
            # WorkflowEnvironment installs a native workflow interceptor.
            config["interceptors"] = []
            client = Client(**config)
            print("Measuring native live execution", file=sys.stderr)
            _, output["native_live"] = await live(
                env,
                client,
                None,
                args.rounds,
                args.runs,
                recorder,
                signals=args.signals,
            )
            print("Measuring WASM live execution", file=sys.stderr)
            histories, output["wasm_live"] = await live(
                env,
                client,
                args.bundle,
                args.rounds,
                args.runs,
                recorder,
                signals=args.signals,
            )
        print("Replaying WASM histories in the native runner", file=sys.stderr)
        output["native_replay"] = await replay(
            histories, None, client.namespace, recorder
        )
        print("Replaying WASM histories in the WASM runner", file=sys.stderr)
        output["wasm_replay"] = await replay(
            histories, args.bundle, client.namespace, recorder
        )
    report = json.dumps(output, indent=2)
    if args.output:
        args.output.write_text(report + "\n")
    else:
        print(report)


if __name__ == "__main__":
    asyncio.run(main())
