"""Capture real histories and replay them in fresh release-bridge processes."""

import argparse
import asyncio
import hashlib
import importlib
import json
import os
import resource
import statistics
import subprocess
import sys
import uuid
from collections import Counter
from pathlib import Path
from threading import Event, Thread
from time import perf_counter

import temporalio.activity as activity
from temporalio.api.enums.v1 import EventType
from temporalio.bridge import temporal_sdk_bridge
from temporalio.client import Client, WorkflowHistory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

ROOT = Path(os.environ["TEMPORAL_ADDRESSABLE_BENCH_DIR"]).resolve()
SDK = Path(__file__).resolve().parents[3]
CLI = os.environ.get("TEMPORAL_DEV_SERVER")
SOURCES = ROOT / "sources"
BUNDLES = ROOT / "bundles"
HISTORIES = ROOT / "histories"
STORE = ROOT / "store"
TYPE = "AddressableWorkflow"
MODULE = "addressable_benchmark_workflow"


@activity.defn(name="addressable_echo")
async def echo(value: int) -> int:
    return value


def save(history: WorkflowHistory, name: str) -> dict:
    path = HISTORIES / f"{name}.json"
    path.write_text(history.to_json())
    by_event = Counter(EventType.Name(event.event_type) for event in history.events)
    versions = Counter()
    metadata_bytes = 0
    for event in history.events:
        if event.event_type != EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED:
            continue
        metadata = event.workflow_task_completed_event_attributes.sdk_metadata
        bindings = metadata.wasm_function_bindings
        if "steps:score" in bindings:
            versions[bindings["steps:score"].hex()] += 1
            metadata_bytes += metadata.ByteSize()
    return {
        "events": len(history.events),
        "event_types": dict(by_event),
        "score_versions": dict(versions),
        "sdk_metadata_bytes_in_bound_tasks": metadata_bytes,
        "history_json_bytes": path.stat().st_size,
    }


async def advance(
    handle, phase: int, start: int, rounds: int, *, count_offset: int
) -> None:
    for local in range(1, rounds + 1):
        await handle.signal("advance", args=[phase, start + local])
        expected = count_offset + local
        while True:
            description = await handle.describe()
            if (
                await description.memo_value("completed_calls", 0, type_hint=int)
                == expected
            ):
                break
            if description.status is not None and description.status.name != "RUNNING":
                await handle.result()
                raise RuntimeError(f"Workflow completed before memo {expected}")
            await asyncio.sleep(0.002)


def publish(phase: int) -> None:
    source = SOURCES / "addressable"
    (source / "steps.py").write_bytes(
        (SOURCES / f"fixed{phase}" / "steps.py").read_bytes()
    )
    subprocess.run(
        [
            sys.executable,
            str(SDK / "scripts/build_workflow_wasm.py"),
            "--source",
            str(source),
            "--workflow",
            f"{MODULE}:{TYPE}",
            "--publish-addressables",
            str(STORE),
        ],
        check=True,
        timeout=30,
    )


def worker(client: Client, queue: str, mode: str) -> Worker:
    return Worker(
        client,
        task_queue=queue,
        wasm_workflow_bundle=BUNDLES / f"{mode}.wasm",
        wasm_addressable_store=STORE if mode == "addressable" else None,
        activities=[echo],
        max_cached_workflows=10,
    )


def expected(phase: int, start: int, rounds: int) -> list:
    return [
        [value + phase * 10, [f"v{phase}", f"{value}/y/z!?"]]
        for value in range(start + 1, start + rounds + 1)
    ]


async def capture(rounds: int) -> None:
    HISTORIES.mkdir(exist_ok=True)
    summary: dict = {
        "rounds_per_version": rounds,
        "capture_bridge": temporal_sdk_bridge.__file__,
        "histories": {},
    }
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=CLI, dev_server_log_level="error"
    ) as env:
        config = env.client.config()
        config["interceptors"] = []
        client = Client(**config)
        summary["namespace"] = client.namespace
        queue = f"addressable-bench-{uuid.uuid4()}"
        all_expected = []
        async with asyncio.timeout(600):
            for phase in range(1, 4):
                publish(phase)
                async with worker(client, queue, "addressable"):
                    if phase == 1:
                        handle = await client.start_workflow(
                            TYPE, 3 * rounds, id=str(uuid.uuid4()), task_queue=queue
                        )
                    start = (phase - 1) * rounds
                    await advance(handle, phase, start, rounds, count_offset=start)
                    all_expected.extend(expected(phase, start, rounds))
                    print(f"Captured addressable phase {phase}", flush=True)
            assert await handle.result() == all_expected
            addressable = save(await handle.fetch_history(), "addressable")
            assert sorted(addressable["score_versions"].values()) == [rounds] * 3, (
                addressable
            )
            summary["histories"]["addressable"] = addressable

            for phase in range(1, 4):
                mode = f"fixed{phase}"
                queue = f"{mode}-bench-{uuid.uuid4()}"
                async with worker(client, queue, mode):
                    handle = await client.start_workflow(
                        TYPE, rounds, id=str(uuid.uuid4()), task_queue=queue
                    )
                    start = (phase - 1) * rounds
                    await advance(handle, phase, start, rounds, count_offset=0)
                    assert await handle.result() == expected(phase, start, rounds)
                    summary["histories"][mode] = save(
                        await handle.fetch_history(), mode
                    )
                print(f"Captured {mode}", flush=True)
    (ROOT / "histories.json").write_text(json.dumps(summary, indent=2) + "\n")


def rss_kib() -> int:
    for field in Path("/proc/self/status").read_text().splitlines():
        if field.startswith("VmRSS:"):
            return int(field.split()[1])
    raise RuntimeError("Linux procfs did not report VmRSS")


async def replay(mode: str, case: str, namespace: str) -> dict:
    history_path = HISTORIES / (
        "addressable.json" if case == "matched" else f"{case}.json"
    )
    history = WorkflowHistory.from_json(history_path.stem, history_path.read_text())
    if mode == "native":
        sys.path.insert(0, str(SOURCES / case))
        workflow = getattr(importlib.import_module(MODULE), TYPE)
        replayer = Replayer(namespace=namespace, workflows=[workflow])
    else:
        replayer = Replayer(
            namespace=namespace,
            wasm_workflow_bundle=BUNDLES / f"{case}.wasm",
            wasm_addressable_store=STORE if case == "addressable" else None,
        )

    async def histories():
        yield history

    peak = 0
    stop = Event()

    def sample_rss() -> None:
        nonlocal peak
        while True:
            peak = max(peak, rss_kib())
            if stop.wait(0.004):
                break

    async with asyncio.timeout(120):
        start = perf_counter()
        async with replayer.workflow_replay_iterator(histories()) as iterator:
            setup = perf_counter() - start
            idle = rss_kib()
            peak = idle
            monitor = Thread(target=sample_rss)
            monitor.start()
            try:
                start = perf_counter()
                result = await anext(iterator)
                elapsed = perf_counter() - start
            finally:
                stop.set()
                monitor.join()
            if result.replay_failure is not None:
                raise result.replay_failure
            peak = max(peak, rss_kib())
    return {
        "setup_ms": setup * 1000,
        "replay_ms": elapsed * 1000,
        "ready_rss_kib": idle,
        "replay_peak_rss_kib": peak,
        "process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }


def measure(trials: int) -> None:
    captures = json.loads((ROOT / "histories.json").read_text())
    histories = captures["histories"]
    assert all(not histories[f"fixed{i}"]["score_versions"] for i in range(1, 4))
    for event in (
        "WORKFLOW_EXECUTION_SIGNALED",
        "WORKFLOW_PROPERTIES_MODIFIED",
        "TIMER_STARTED",
        "ACTIVITY_TASK_SCHEDULED",
    ):
        event = f"EVENT_TYPE_{event}"
        fixed = sum(
            histories[f"fixed{i}"]["event_types"].get(event, 0) for i in range(1, 4)
        )
        assert histories["addressable"]["event_types"][event] == fixed, event
    cases = [
        ("wasm", "addressable"),
        ("wasm", "matched"),
        *(("wasm", f"fixed{i}") for i in range(1, 4)),
        *(("native", f"fixed{i}") for i in range(1, 4)),
    ]
    groups = {
        "addressable": [("wasm", "addressable")],
        "matched": [("wasm", "matched")],
        "wasm_fixed_series": [("wasm", f"fixed{i}") for i in range(1, 4)],
        "native_fixed_series": [("native", f"fixed{i}") for i in range(1, 4)],
    }
    samples = []
    for trial in range(trials):
        runs = {}
        for mode, case in cases[trial:] + cases[:trial]:
            output = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "replay",
                    "--mode",
                    mode,
                    "--case",
                    case,
                    "--namespace",
                    captures["namespace"],
                ],
                capture_output=True,
                text=True,
                timeout=180,
            )
            if output.returncode:
                print(output.stderr, file=sys.stderr)
                output.check_returncode()
            sample = json.loads(output.stdout)
            runs[mode, case] = sample
            print(f"Trial {trial + 1} {mode}/{case}: {sample}", flush=True)
        aggregate = {}
        for group, members in groups.items():
            phases = [runs[member] for member in members]
            aggregate[group] = {
                "setup_ms": sum(p["setup_ms"] for p in phases),
                "replay_ms": sum(p["replay_ms"] for p in phases),
                "ready_rss_kib": max(p["ready_rss_kib"] for p in phases),
                "replay_peak_rss_kib": max(p["replay_peak_rss_kib"] for p in phases),
                "process_peak_rss_kib": max(p["process_peak_rss_kib"] for p in phases),
                "replay_added_rss_kib": max(
                    p["replay_peak_rss_kib"] - p["ready_rss_kib"] for p in phases
                ),
            }
        samples.append(
            {
                "individual": {
                    f"{mode}/{case}": value for (mode, case), value in runs.items()
                },
                "aggregate": aggregate,
            }
        )
    medians = {
        group: {
            key: statistics.median(
                sample["aggregate"][group][key] for sample in samples
            )
            for key in samples[0]["aggregate"][group]
        }
        for group in groups
    }
    result = {
        "bridge": temporal_sdk_bridge.__file__,
        "bridge_build": "release opt-level 3 with fat LTO and wasm-workflows",
        "bridge_sha256": hashlib.sha256(
            Path(temporal_sdk_bridge.__file__).read_bytes()
        ).hexdigest(),
        "trials": trials,
        **captures,
        "medians": medians,
        "samples": samples,
    }
    (ROOT / "results.json").write_text(json.dumps(result, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["capture", "measure", "replay"])
    parser.add_argument("--rounds", type=int, default=300)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--mode")
    parser.add_argument("--case")
    parser.add_argument("--namespace")
    args = parser.parse_args()
    bridge = Path(temporal_sdk_bridge.__file__).resolve()
    assert hasattr(temporal_sdk_bridge, "WasmWorkflowBundle")
    if args.action == "capture":
        asyncio.run(capture(args.rounds))
        return
    assert bridge.is_relative_to(ROOT / "release"), bridge
    if args.action == "measure":
        measure(args.trials)
    else:
        print(json.dumps(asyncio.run(replay(args.mode, args.case, args.namespace))))


if __name__ == "__main__":
    main()
