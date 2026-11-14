import ast
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import gen_protos

base_dir = Path(__file__).parent.parent
sys.path.insert(0, str(base_dir))
wit_input_dir = (
    base_dir
    / "temporalio"
    / "bridge"
    / "sdk-core"
    / "crates"
    / "protos"
    / "protos"
    / "api_upstream"
    / "nexus"
)
wit_deps_dir = wit_input_dir / "deps"
workflow_output_dir = base_dir / "temporalio" / "nexus" / "system" / "workflow_service"
stream_output_dir = base_dir / "temporalio" / "nexus" / "system" / "stream_service"
notification_output_dir = base_dir / "temporalio" / "nexus" / "notifications"
workflow_init_path = base_dir / "temporalio" / "workflow" / "__init__.py"
NEX_GEN_VERSION = "0.2.8"
# The Stream package needs a newer nexgen, which rejects the other WITs. One pin is enough
# again once the api WITs and a nexgen release agree. No release has nexgen commit 9653a0f5,
# the `main` of nexgen, yet, so NEX_GEN_STREAM_BIN points at a build of it.
NEX_GEN_STREAM_VERSION = "0.2.12"
proto_files = [
    gen_protos.api_proto_dir
    / "temporal"
    / "api"
    / "workflowservice"
    / "v1"
    / "request_response.proto",
    gen_protos.api_proto_dir
    / "temporal"
    / "api"
    / "streamservice"
    / "v1"
    / "request_response.proto",
    gen_protos.api_proto_dir
    / "temporal"
    / "api"
    / "notificationservice"
    / "v1"
    / "request_response.proto",
]
# The Workflow functions of these generated packages are exported by temporalio.workflow.
# They are in import order, so that sorting the imports keeps them between the markers.
workflow_export_packages = [
    ("temporalio.nexus.system.stream_service.workflow", stream_output_dir / "workflow"),
    ("temporalio.nexus.system.workflow_service", workflow_output_dir),
]


def nex_gen_command() -> list[str]:
    if bin_path := os.environ.get("NEX_GEN_BIN"):
        return [bin_path]
    if (
        shutil.which("nexgen") is None
        or subprocess.check_output(["nexgen", "--version"], text=True).strip()
        != f"nexgen {NEX_GEN_VERSION}"
    ):
        subprocess.check_call(
            [
                "cargo",
                "install",
                "--locked",
                "nexgen",
                "--version",
                NEX_GEN_VERSION,
                "--features",
                "advanced",
                "--force",
            ]
        )
    return ["nexgen"]


def nex_gen_stream_command() -> list[str]:
    bin_path = os.environ.get("NEX_GEN_STREAM_BIN")
    if not bin_path:
        raise RuntimeError(
            "NEX_GEN_STREAM_BIN must point at a nexgen build of commit 9653a0f5"
        )
    version = subprocess.check_output([bin_path, "--version"], text=True).strip()
    if version != f"nexgen {NEX_GEN_STREAM_VERSION}":
        raise RuntimeError(
            f"NEX_GEN_STREAM_BIN is {version}, not nexgen {NEX_GEN_STREAM_VERSION}"
        )
    return [bin_path]


def build_descriptor_set(descriptor_path: Path) -> None:
    subprocess.check_call(
        [
            "protoc",
            f"--proto_path={gen_protos.api_proto_dir}",
            f"--proto_path={gen_protos.proto_dir}",
            "--include_imports",
            f"--descriptor_set_out={descriptor_path}",
            *map(str, proto_files),
        ]
    )


def generate_package(
    command: list[str],
    wit_name: str,
    output_dir: Path,
    descriptor_path: Path,
    *,
    flags: list[str],
) -> None:
    subprocess.check_call(
        [
            *command,
            "python",
            str(wit_input_dir / wit_name),
            str(wit_deps_dir),
            *flags,
            "--system-nexus",
            "--support-package",
            "temporalio.nexus.system._support",
            "--descriptors",
            str(descriptor_path),
            "--output",
            str(output_dir),
        ]
    )


def generated_exports(init_path: Path) -> list[str]:
    tree = ast.parse(init_path.read_text())
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or not any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in statement.targets
        ):
            continue
        value = ast.literal_eval(statement.value)
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise RuntimeError(
                f"generated package {init_path} __all__ must be a list of strings"
            )
        return value
    raise RuntimeError(f"generated package {init_path} does not define __all__")


def replace_marker_block(
    content: str, begin: str, end: str, replacement: list[str]
) -> str:
    start = content.index(begin)
    finish = content.index("\n", content.index(end, start)) + 1
    return content[:start] + "".join(replacement) + content[finish:]


def generate_workflow_exports() -> None:
    package_exports = [
        (module, generated_exports(package_dir / "__init__.py"))
        for module, package_dir in workflow_export_packages
    ]
    import_block = [
        "# BEGIN GENERATED NEXUS SYSTEM EXPORTS\n",
        *[
            line
            for module, exports in package_exports
            for line in [
                f"from {module} import (\n",
                *[f"    {export},\n" for export in exports],
                ")\n",
            ]
        ],
        "# END GENERATED NEXUS SYSTEM EXPORTS\n",
    ]
    all_block = [
        "    # BEGIN GENERATED NEXUS SYSTEM __ALL__\n",
        *[f'    "{export}",\n' for _, exports in package_exports for export in exports],
        "    # END GENERATED NEXUS SYSTEM __ALL__\n",
    ]
    content = workflow_init_path.read_text()
    content = replace_marker_block(
        content, import_block[0].strip(), import_block[-1].strip(), import_block
    )
    workflow_init_path.write_text(
        replace_marker_block(
            content,
            "    # BEGIN GENERATED NEXUS SYSTEM __ALL__",
            "    # END GENERATED NEXUS SYSTEM __ALL__",
            all_block,
        )
    )


def generate_nexus_system_api() -> None:
    required_paths = [
        wit_input_dir / "workflow-service.wit",
        wit_input_dir / "stream-service.wit",
        wit_input_dir / "notification-service.wit",
        wit_deps_dir,
        *proto_files,
    ]
    for path in required_paths:
        if not path.exists():
            raise RuntimeError(f"missing generator input: {path}")
    with tempfile.TemporaryDirectory(dir=base_dir) as temp_dir:
        descriptor_path = Path(temp_dir) / "temporal_api.bin"
        build_descriptor_set(descriptor_path)
        command = nex_gen_command()
        stream_command = nex_gen_stream_command()
        for wit_name, output_dir, package_command, flags in (
            ("workflow-service.wit", workflow_output_dir, command, ["--native-api"]),
            (
                "stream-service.wit",
                stream_output_dir,
                stream_command,
                ["--targets", "workflow,client"],
            ),
            ("notification-service.wit", notification_output_dir, command, []),
        ):
            shutil.rmtree(output_dir, ignore_errors=True)
            output_dir.parent.mkdir(parents=True, exist_ok=True)
            generate_package(
                package_command,
                wit_name,
                output_dir,
                descriptor_path,
                flags=flags,
            )
    workflow_output_dir.parent.joinpath("__init__.py").touch()
    generate_workflow_exports()
    format_paths = [
        str(workflow_output_dir),
        str(stream_output_dir),
        str(notification_output_dir),
        str(workflow_init_path),
    ]
    subprocess.check_call(
        [sys.executable, "-m", "ruff", "check", "--select", "I", "--fix", *format_paths]
    )
    subprocess.check_call([sys.executable, "-m", "ruff", "format", *format_paths])


if __name__ == "__main__":
    print("Generating Nexus system API...", file=sys.stderr)
    generate_nexus_system_api()
    print("Done", file=sys.stderr)
