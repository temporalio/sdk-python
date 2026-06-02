import os
import shutil
import subprocess
import sys
import tempfile
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import cast

import gen_protos

base_dir = Path(__file__).parent.parent
sys.path.insert(0, str(base_dir))
wit_input_dir = base_dir / "scripts" / "_nexus"
wit_path = wit_input_dir / "temporal-system.wit"
wit_deps_dir = wit_input_dir / "deps"
output_dir = base_dir / "temporalio" / "nexus" / "system" / "workflow_service"
workflow_init_path = base_dir / "temporalio" / "workflow" / "__init__.py"
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

    if shutil.which("nex-gen") is None:
        subprocess.check_call(["cargo", "install", "--locked", "nex-gen"])
    return ["nex-gen"]


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


def generate_workflow_exports() -> None:
    spec = spec_from_file_location(
        "temporalio_nexus_system_workflow_service_exports",
        output_dir / "__init__.py",
        submodule_search_locations=[str(output_dir)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load generated workflow service from {output_dir}")
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    exports = cast(list[str], module.__all__)

    import_block = [
        "# BEGIN GENERATED NEXUS SYSTEM EXPORTS\n",
        "from temporalio.nexus.system.workflow_service import (\n",
        *[f"    {export},\n" for export in exports],
        ")\n",
        "# END GENERATED NEXUS SYSTEM EXPORTS\n",
    ]
    all_block = [
        "    # BEGIN GENERATED NEXUS SYSTEM __ALL__\n",
        *[f'    "{export}",\n' for export in exports],
        "    # END GENERATED NEXUS SYSTEM __ALL__\n",
    ]
    content = workflow_init_path.read_text()
    start = content.index("# BEGIN GENERATED NEXUS SYSTEM EXPORTS")
    end = content.index("# END GENERATED NEXUS SYSTEM EXPORTS", start)
    end = content.index("\n", end) + 1
    content = content[:start] + "".join(import_block) + content[end:]
    start = content.index("    # BEGIN GENERATED NEXUS SYSTEM __ALL__")
    end = content.index("    # END GENERATED NEXUS SYSTEM __ALL__", start)
    end = content.index("\n", end) + 1
    workflow_init_path.write_text(content[:start] + "".join(all_block) + content[end:])


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
    generate_workflow_exports()
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
            str(workflow_init_path),
        ]
    )
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            str(output_dir),
            str(workflow_init_path),
        ]
    )


if __name__ == "__main__":
    print("Generating Nexus system API...", file=sys.stderr)
    generate_nexus_system_api()
    print("Done", file=sys.stderr)
