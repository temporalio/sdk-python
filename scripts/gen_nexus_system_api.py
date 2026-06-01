import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import gen_protos

base_dir = Path(__file__).parent.parent
wit_input_dir = base_dir / "scripts" / "_nexus"
wit_path = wit_input_dir / "temporal-system.wit"
wit_deps_dir = wit_input_dir / "deps"
output_dir = base_dir / "temporalio" / "nexus" / "system" / "workflow_service"
default_nex_gen_install_root = (
    Path(tempfile.gettempdir()) / "temporal-sdk-python-nex-gen"
)
workflowservice_request_response_proto = (
    gen_protos.api_proto_dir
    / "temporal"
    / "api"
    / "workflowservice"
    / "v1"
    / "request_response.proto"
)


def nex_gen_command() -> list[str]:
    if bin_path := os.environ.get("NEX_GEN_BIN"):
        return [bin_path]

    return [str(install_published_nex_gen())]


def install_published_nex_gen() -> Path:
    install_root = Path(
        os.environ.get(
            "NEX_GEN_INSTALL_ROOT",
            str(default_nex_gen_install_root),
        )
    )
    bin_name = "nex-gen.exe" if os.name == "nt" else "nex-gen"
    bin_path = install_root / "bin" / bin_name
    if not bin_path.exists():
        subprocess.check_call(
            [
                "cargo",
                "install",
                "--locked",
                "--root",
                str(install_root),
                "nex-gen",
            ]
        )
    return bin_path


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
    if not wit_deps_dir.exists():
        raise RuntimeError(f"missing WIT dependency directory: {wit_deps_dir}")

    with tempfile.TemporaryDirectory(dir=base_dir) as temp_dir:
        descriptor_path = Path(temp_dir) / "temporal_api.bin"
        build_descriptor_set(descriptor_path)
        command = nex_gen_command()

        shutil.rmtree(output_dir, ignore_errors=True)
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.check_call(
            [
                *command,
                "generate",
                "--lang",
                "python",
                "--input",
                str(wit_path),
                "--input",
                str(wit_deps_dir),
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
