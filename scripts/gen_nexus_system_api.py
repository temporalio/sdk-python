import ast
import os
import re
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
python_support_path = base_dir / "scripts" / "nex_gen_support.py"
workflow_output_dir = base_dir / "temporalio" / "nexus" / "system" / "workflow_service"
notification_output_dir = base_dir / "temporalio" / "nexus" / "notifications"
support_output_dir = base_dir / "temporalio" / "nexus" / "_support"
workflow_init_path = base_dir / "temporalio" / "workflow" / "__init__.py"
NEX_GEN_VERSION = "0.2.7"
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
            sys.executable,
            "-mgrpc_tools.protoc",
            f"--proto_path={gen_protos.api_proto_dir}",
            f"--proto_path={gen_protos.proto_dir}",
            "--include_imports",
            f"--descriptor_set_out={descriptor_path}",
            *map(str, proto_files),
        ]
    )


def generate_package(
    command: list[str], wit_name: str, output_dir: Path, *, native_api: bool
) -> None:
    args = [*command, "python", str(wit_input_dir / wit_name), str(wit_deps_dir)]
    if native_api:
        args.append("--native-api")
    subprocess.check_call(
        [
            *args,
            "--system-nexus",
            "--support-file",
            str(python_support_path),
            "--descriptors",
            str(output_dir.parent / "temporal_api.bin"),
            "--output",
            str(output_dir),
        ]
    )


def merge_support_trees(support_dirs: list[Path], destination: Path) -> None:
    merged_files: dict[Path, Path] = {}
    for support_dir in support_dirs:
        if not support_dir.is_dir():
            raise RuntimeError(
                f"generator did not produce support directory: {support_dir}"
            )
        for source in support_dir.rglob("*"):
            if not source.is_file():
                continue
            relative_path = source.relative_to(support_dir)
            if previous := merged_files.get(relative_path):
                if previous.read_bytes() != source.read_bytes():
                    raise RuntimeError(
                        f"generated support files differ at {relative_path}: {previous} and {source}"
                    )
            else:
                merged_files[relative_path] = source
    destination.mkdir(parents=True, exist_ok=True)
    for relative_path, source in merged_files.items():
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def rewrite_support_imports(output_dir: Path) -> None:
    for source in output_dir.rglob("*.py"):
        if "_support" in source.relative_to(output_dir).parts:
            continue
        content = source.read_text()
        rewritten = re.sub(
            r"from \._support(\.[A-Za-z_][A-Za-z0-9_]*)? import ",
            r"from temporalio.nexus._support\1 import ",
            content,
        )
        if rewritten != content:
            source.write_text(rewritten)


def workflow_exports() -> list[str]:
    tree = ast.parse((workflow_output_dir / "__init__.py").read_text())
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
                "generated workflow package __all__ must be a list of strings"
            )
        return value
    raise RuntimeError("generated workflow package does not define __all__")


def replace_marker_block(
    content: str, begin: str, end: str, replacement: list[str]
) -> str:
    start = content.index(begin)
    finish = content.index("\n", content.index(end, start)) + 1
    return content[:start] + "".join(replacement) + content[finish:]


def generate_workflow_exports() -> None:
    exports = workflow_exports()
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


def publish_generated_packages(
    staged_workflow: Path, staged_notification: Path
) -> None:
    staged_support_dirs = [
        staged_workflow / "_support",
        staged_notification / "_support",
    ]
    staged_support = staged_workflow.parent / "_support"
    merge_support_trees(staged_support_dirs, staged_support)
    for output_dir in (staged_workflow, staged_notification):
        rewrite_support_imports(output_dir)
        shutil.rmtree(output_dir / "_support")
    for output_dir in (
        workflow_output_dir,
        notification_output_dir,
        support_output_dir,
    ):
        shutil.rmtree(output_dir, ignore_errors=True)
    workflow_output_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(staged_workflow, workflow_output_dir)
    notification_output_dir.mkdir(parents=True)
    shutil.copy2(
        staged_notification / "models.py", notification_output_dir / "models.py"
    )
    notification_output_dir.joinpath("__init__.py").write_text(
        "from .models import OnCompleteRequest, OnCompleteRequestResult, OnCompleteResponse\n\n"
        "__all__ = [\n"
        '    "OnCompleteRequest",\n'
        '    "OnCompleteRequestResult",\n'
        '    "OnCompleteResponse",\n'
        "]\n"
    )
    shutil.copytree(staged_support, support_output_dir)
    workflow_output_dir.parent.joinpath("__init__.py").touch()


def generate_nexus_system_api() -> None:
    required_paths = [
        wit_input_dir / "workflow-service.wit",
        wit_input_dir / "notification-service.wit",
        wit_deps_dir,
        python_support_path,
        *proto_files,
    ]
    for path in required_paths:
        if not path.exists():
            raise RuntimeError(f"missing generator input: {path}")
    with tempfile.TemporaryDirectory(dir=base_dir) as temp_dir:
        staging_dir = Path(temp_dir)
        descriptor_path = staging_dir / "temporal_api.bin"
        build_descriptor_set(descriptor_path)
        command = nex_gen_command()
        staged_workflow = staging_dir / "workflow_service"
        staged_notification = staging_dir / "notifications"
        generate_package(
            command, "workflow-service.wit", staged_workflow, native_api=True
        )
        generate_package(
            command, "notification-service.wit", staged_notification, native_api=False
        )
        publish_generated_packages(staged_workflow, staged_notification)
    generate_workflow_exports()
    format_paths = [
        str(workflow_output_dir),
        str(notification_output_dir),
        str(support_output_dir),
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
