from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

NEXUS_RPC_GEN_ENV_VAR = "TEMPORAL_NEXUS_RPC_GEN_DIR"
NEXUS_RPC_GEN_VERSION = "0.1.0-alpha.4"


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    # TODO: Remove the local .nexusrpc.yaml shim once the upstream API repo
    # checks in the Nexus definition we can consume directly.
    override_root = normalize_nexus_rpc_gen_root(
        Path.cwd(), env_value=NEXUS_RPC_GEN_ENV_VAR
    )
    input_schema = (
        repo_root
        / "temporalio"
        / "bridge"
        / "sdk-core"
        / "crates"
        / "common"
        / "protos"
        / "api_upstream"
        / "nexus"
        / "temporal-proto-models-nexusrpc.yaml"
    )
    output_file = (
        repo_root / "temporalio" / "nexus" / "system" / "_workflow_service_generated.py"
    )

    if not input_schema.is_file():
        raise RuntimeError(f"Expected Nexus schema at {input_schema}")

    run_nexus_rpc_gen(
        override_root=override_root,
        output_file=output_file,
        input_schema=input_schema,
    )
    subprocess.run(
        [
            "uv",
            "run",
            "ruff",
            "check",
            "--select",
            "I",
            "--fix",
            str(output_file),
        ],
        cwd=repo_root,
        check=True,
    )
    subprocess.run(
        [
            "uv",
            "run",
            "ruff",
            "format",
            str(output_file),
        ],
        cwd=repo_root,
        check=True,
    )


def run_nexus_rpc_gen(
    *, override_root: Path | None, output_file: Path, input_schema: Path
) -> None:
    common_args = [
        "--lang",
        "py",
        "--out-file",
        str(output_file),
        str(input_schema),
    ]
    if override_root is None:
        subprocess.run(
            ["npx", "--yes", f"nexus-rpc-gen@{NEXUS_RPC_GEN_VERSION}", *common_args],
            check=True,
        )
        return

    subprocess.run(
        [
            "node",
            "packages/nexus-rpc-gen/dist/index.js",
            *common_args,
        ],
        cwd=override_root,
        check=True,
    )


def normalize_nexus_rpc_gen_root(base_dir: Path, env_value: str) -> Path | None:
    raw_root = env_get(env_value)
    if raw_root is None:
        return None
    candidate = Path(raw_root)
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    candidate = candidate.resolve()
    if (candidate / "package.json").is_file() and (candidate / "packages").is_dir():
        return candidate
    if (candidate / "src" / "package.json").is_file():
        return candidate / "src"
    raise RuntimeError(
        f"{NEXUS_RPC_GEN_ENV_VAR} must point to the nexus-rpc-gen repo root or its src directory"
    )


def env_get(name: str) -> str | None:
    return os.environ.get(name)


if __name__ == "__main__":
    try:
        main()
    except Exception as err:
        print(f"Failed to generate Nexus system models: {err}", file=sys.stderr)
        raise
