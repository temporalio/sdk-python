"""Scale the rollout fixture to a long history and package the benchmark bundles."""

import concurrent.futures
import os
import subprocess
from pathlib import Path

ROOT = Path(os.environ["TEMPORAL_ADDRESSABLE_BENCH_DIR"]).resolve()
SDK = Path(__file__).resolve().parents[3]
FIXTURE = SDK / "tests/worker/wasm_fixtures/addressable"
SOURCES = ROOT / "sources"
BUNDLES = ROOT / "bundles"
COMPONENTIZER = os.environ["TEMPORAL_WASM_COMPONENTIZER_PYTHON"]
DEPS = os.environ["TEMPORAL_WASM_DEPENDENCY_PATH"]


def workflow_source() -> str:
    text, _, _ = (
        (FIXTURE / "workflows.py")
        .read_text()
        .partition("\n\n@workflow.defn\nclass AddressableBatch")
    )
    return (
        text.replace("import steps as scoring  # type: ignore[import-not-found]\n", "")
        .replace(
            "def expected_labels(value: int)",
            "def expected_labels(phase: int, value: int)",
        )
        .replace('f"v{value}"', 'f"v{phase}"')
        .replace("self.inputs: list[int]", "self.inputs: list[tuple[int, int]]")
        .replace(
            "def advance(self, value: int)", "def advance(self, phase: int, value: int)"
        )
        .replace("self.inputs.append(value)", "self.inputs.append((phase, value))")
        .replace("return remembered()", "return remembered(0)")
        .replace("async def run(self)", "async def run(self, rounds: int)")
        .replace("len(results) < 3", "len(results) < rounds")
        .replace(
            "value = self.inputs[len(results)]",
            "phase, value = self.inputs[len(results)]",
        )
        .replace("remembered()", "remembered(value)")
        .replace("expected_labels(value)", "expected_labels(phase, value)")
        + "\n"
    )


def steps_source(version: int) -> str:
    return (
        (FIXTURE / f"v{version}/steps.py")
        .read_text()
        .replace(
            "    await workflow.sleep(timedelta(seconds=5))",
            "    if value % 50 == 0:\n        await workflow.sleep(timedelta(milliseconds=1))",
        )
        .replace(
            "    await workflow.sleep(timedelta(milliseconds=10))",
            "    if value % 50 == 0:\n        await workflow.sleep(timedelta(milliseconds=1))",
        )
        .replace(
            "@lru_cache\ndef remembered()",
            "@lru_cache(maxsize=1)\ndef remembered(value: int)",
        )
    )


def build(name: str) -> None:
    cmd = [
        COMPONENTIZER,
        str(SDK / "scripts/build_workflow_wasm.py"),
        "--source",
        str(SOURCES / name),
        "--dependency-path",
        DEPS,
        "--workflow",
        "addressable_benchmark_workflow:AddressableWorkflow",
        "--output",
        str(BUNDLES / f"{name}.wasm"),
    ]
    if name == "addressable":
        cmd.append("--addressable")
    subprocess.run(cmd, env=os.environ.copy(), cwd=SDK, check=True, timeout=150)
    print(f"Built {name}", flush=True)


def main() -> None:
    BUNDLES.mkdir(parents=True, exist_ok=True)
    workflow = workflow_source()
    for name, version in (
        ("addressable", 1),
        ("fixed1", 1),
        ("fixed2", 2),
        ("fixed3", 3),
    ):
        root = SOURCES / name
        root.mkdir(parents=True, exist_ok=True)
        (root / "addressable_benchmark_workflow.py").write_text(workflow)
        (root / "steps.py").write_text(steps_source(version))

    matched = SOURCES / "matched"
    matched.mkdir(parents=True, exist_ok=True)
    for version in range(1, 4):
        (matched / f"steps_v{version}.py").write_text(steps_source(version))
    matched_workflow = workflow.replace(
        "from steps import remember, remembered  # type: ignore[import-not-found]\n"
        "from steps import score as calculate_score  # type: ignore[import-not-found]",
        "import steps_v1\nimport steps_v2\nimport steps_v3\n\n_versions = (steps_v1, steps_v2, steps_v3)",
    ).replace(
        "            remember(str(value)",
        "            steps = _versions[phase - 1]\n            steps.remember(str(value)",
    )
    matched_workflow = (
        matched_workflow.replace(
            "return remembered(0)", "return steps_v1.remembered(0)"
        )
        .replace("remembered(value)", "steps.remembered(value)")
        .replace("await calculate_score(", "await steps.score(")
    )
    (matched / "addressable_benchmark_workflow.py").write_text(matched_workflow)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(build, ("addressable", "fixed1", "fixed2", "fixed3", "matched")))


if __name__ == "__main__":
    main()
