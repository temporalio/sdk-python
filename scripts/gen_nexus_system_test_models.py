from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    workspace_root = repo_root.parent
    nexus_rpc_gen_root = workspace_root / "nexus-rpc-gen" / "src"
    input_schema = (
        workspace_root
        / "temporal-api"
        / "nexus"
        / "temporal-json-schema-models-nexusrpc.yaml"
    )
    output_file = (
        repo_root / "temporalio" / "nexus" / "system" / "_workflow_service_generated.py"
    )

    if not nexus_rpc_gen_root.is_dir():
        raise RuntimeError(f"Expected nexus-rpc-gen checkout at {nexus_rpc_gen_root}")
    if not input_schema.is_file():
        raise RuntimeError(f"Expected Temporal Nexus schema at {input_schema}")

    subprocess.run(
        [
            "npm",
            "run",
            "cli",
            "--",
            "--lang",
            "py",
            "--out-file",
            str(output_file),
            "--temporal-nexus-payload-codec-support",
            str(input_schema),
        ],
        cwd=nexus_rpc_gen_root,
        check=True,
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


if __name__ == "__main__":
    try:
        main()
    except Exception as err:
        print(f"Failed to generate Nexus system test models: {err}", file=sys.stderr)
        raise
