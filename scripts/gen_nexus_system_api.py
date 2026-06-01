import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import gen_protos

base_dir = Path(__file__).parent.parent
wit_path = base_dir / "scripts" / "_nexus" / "temporal-system.wit"
output_dir = base_dir / "temporalio" / "nexus" / "_system" / "workflow_service"
workflowservice_request_response_proto = (
    gen_protos.api_proto_dir
    / "temporal"
    / "api"
    / "workflowservice"
    / "v1"
    / "request_response.proto"
)


def nexus_api_gen_command() -> list[str]:
    if bin_path := os.environ.get("NEXUS_API_GEN_BIN"):
        return [bin_path]

    candidates = []
    if source_dir := os.environ.get("NEXUS_API_GEN_DIR"):
        candidates.append(Path(source_dir))
    candidates.extend(
        [
            base_dir / "tools" / "nexus-api-gen",
            base_dir.parent / "nexus-api-gen",
        ]
    )
    for candidate in candidates:
        manifest_path = candidate / "Cargo.toml"
        if manifest_path.exists():
            return ["cargo", "run", "--manifest-path", str(manifest_path), "--"]

    raise RuntimeError(
        "nexus-api-gen not found; set NEXUS_API_GEN_BIN or NEXUS_API_GEN_DIR"
    )


def build_descriptor_set(descriptor_path: Path) -> None:
    subprocess.check_call(
        [
            sys.executable,
            "-mgrpc_tools.protoc",
            f"--proto_path={gen_protos.api_proto_dir}",
            f"--proto_path={gen_protos.proto_dir}",
            "--include_imports",
            f"--descriptor_set_out={descriptor_path}",
            str(workflowservice_request_response_proto),
        ]
    )


def strip_unsupported_pyright_comments() -> None:
    for path in output_dir.rglob("*.py"):
        content = path.read_text()
        content = content.replace("# pyright: reportAny=false\n", "")
        content = content.replace(
            "# pyright: reportAny=false, reportExplicitAny=false\n", ""
        )
        path.write_text(content)


def generate_nexus_system_api() -> None:
    if not wit_path.exists():
        raise RuntimeError(f"missing WIT source: {wit_path}")

    with tempfile.TemporaryDirectory(dir=base_dir) as temp_dir:
        descriptor_path = Path(temp_dir) / "temporal_api.bin"
        build_descriptor_set(descriptor_path)

        shutil.rmtree(output_dir, ignore_errors=True)
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.check_call(
            [
                *nexus_api_gen_command(),
                "generate",
                "--lang",
                "python",
                "--input",
                str(wit_path),
                "--descriptors",
                str(descriptor_path),
                "--output",
                str(output_dir),
            ]
        )

    (output_dir.parent / "__init__.py").touch()
    strip_unsupported_pyright_comments()
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "I",
            "--fix",
            str(output_dir),
        ]
    )
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            str(output_dir),
        ]
    )


if __name__ == "__main__":
    print("Generating Nexus system API...", file=sys.stderr)
    generate_nexus_system_api()
    print("Done", file=sys.stderr)
