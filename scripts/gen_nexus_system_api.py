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
notification_output_dir = base_dir / "temporalio" / "nexus" / "notifications"
stream_output_dir = base_dir / "temporalio" / "nexus" / "system" / "stream_service"
workflow_init_path = base_dir / "temporalio" / "workflow" / "__init__.py"
NEX_GEN_VERSION = "0.2.14"
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
    / "notificationservice"
    / "v1"
    / "request_response.proto",
    gen_protos.api_proto_dir
    / "temporal"
    / "api"
    / "streamservice"
    / "v1"
    / "request_response.proto",
    gen_protos.api_proto_dir / "temporal" / "api" / "sdk" / "v1" / "stream.proto",
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
    targets: str | None,
) -> None:
    args = [*command, "python", str(wit_input_dir / wit_name), str(wit_deps_dir)]
    if targets:
        args.extend(["--targets", targets])
    subprocess.check_call(
        [
            *args,
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
    packages = [
        (
            "temporalio.nexus.system.stream_service.workflow",
            stream_output_dir / "workflow" / "__init__.py",
        ),
        (
            "temporalio.nexus.system.workflow_service",
            workflow_output_dir / "__init__.py",
        ),
    ]
    exports = [(module, generated_exports(path)) for module, path in packages]
    import_block = ["# BEGIN GENERATED NEXUS SYSTEM EXPORTS\n"]
    for module, names in exports:
        import_block += [
            f"from {module} import (\n",
            *[f"    {name},\n" for name in names],
            ")\n",
        ]
    import_block.append("# END GENERATED NEXUS SYSTEM EXPORTS\n")
    all_block = [
        "    # BEGIN GENERATED NEXUS SYSTEM __ALL__\n",
        *[f'    "{name}",\n' for _, names in exports for name in names],
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
        wit_input_dir / "notification-service.wit",
        wit_input_dir / "stream-service.wit",
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
        for wit_name, output_dir, targets in (
            ("workflow-service.wit", workflow_output_dir, "workflow"),
            ("notification-service.wit", notification_output_dir, None),
            ("stream-service.wit", stream_output_dir, "workflow,client"),
        ):
            shutil.rmtree(output_dir, ignore_errors=True)
            output_dir.parent.mkdir(parents=True, exist_ok=True)
            generate_package(
                command,
                wit_name,
                output_dir,
                descriptor_path,
                targets=targets,
            )
    workflow_output_dir.parent.joinpath("__init__.py").touch()
    generate_workflow_exports()
    format_paths = [
        str(workflow_output_dir),
        str(notification_output_dir),
        str(stream_output_dir),
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
