from __future__ import annotations

import importlib
import re
import subprocess
import sys
from pathlib import Path

NEXUS_RPC_GEN_VERSION = "0.1.0-alpha.4"


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    # TODO: Remove the local .nexusrpc.yaml shim once the upstream API repo
    # checks in the Nexus definition we can consume directly.
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
        output_file=output_file,
        input_schema=input_schema,
    )
    add_operation_registry(repo_root, output_file)
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


def add_operation_registry(repo_root: Path, output_file: Path) -> None:
    source = strip_existing_operation_registry(output_file.read_text())
    source = ensure_typing_import(source)
    services = discover_services(repo_root)
    if not services:
        output_file.write_text(source)
        return
    output_file.write_text(source.rstrip() + "\n\n" + emit_operation_registry(services))


def strip_existing_operation_registry(source: str) -> str:
    source = re.sub(
        r"\nimport typing\n(?=\n__nexus_operation_registry__)",
        "\n",
        source,
    )
    source = re.sub(
        r"\n__nexus_operation_registry__: dict\[\n"
        r"(?:.*\n)*?"
        r"\] = \{\}\n"
        r"(?:\n__nexus_operation_registry__\[\n(?:.*\n)*?\] = .+\n)+",
        "\n",
        source,
        flags=re.MULTILINE,
    )
    return source.rstrip() + "\n"


def ensure_typing_import(source: str) -> str:
    if "\nimport typing\n" in source:
        return source
    marker = "from __future__ import annotations\n\n"
    if marker not in source:
        raise RuntimeError("Expected future-annotations import in generated output")
    return source.replace(marker, marker + "import typing\n", 1)


def discover_services(repo_root: Path) -> list[tuple[str, str, list[tuple[str, str]]]]:
    module_name = "temporalio.nexus.system._workflow_service_generated"
    sys.path.insert(0, str(repo_root))
    try:
        sys.modules.pop(module_name, None)
        importlib.invalidate_caches()
        module = importlib.import_module(module_name)
    finally:
        sys.path.pop(0)
    services: list[tuple[str, str, list[tuple[str, str]]]] = []
    for value in vars(module).values():
        if not isinstance(value, type):
            continue
        definition = getattr(value, "__nexus_service_definition__", None)
        if definition is None:
            continue
        operations = [
            (operation_definition.method_name, operation_definition.name)
            for operation_definition in definition.operation_definitions.values()
        ]
        services.append((value.__name__, definition.name, operations))
    return services


def emit_operation_registry(
    services: list[tuple[str, str, list[tuple[str, str]]]],
) -> str:
    lines = [
        "__nexus_operation_registry__: dict[",
        "    tuple[str, str], Operation[typing.Any, typing.Any]",
        "] = {}",
        "",
    ]
    for class_name, service_name, operations in services:
        for attr_name, operation_name in operations:
            lines.extend(
                [
                    "__nexus_operation_registry__[",
                    f"    ({service_name!r}, {operation_name!r})",
                    f"] = {class_name}.{attr_name}",
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def run_nexus_rpc_gen(*, output_file: Path, input_schema: Path) -> None:
    common_args = [
        "--lang",
        "py",
        "--out-file",
        str(output_file),
        str(input_schema),
    ]
    subprocess.run(
        ["npx", "--yes", f"nexus-rpc-gen@{NEXUS_RPC_GEN_VERSION}", *common_args],
        check=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as err:
        print(f"Failed to generate Nexus system models: {err}", file=sys.stderr)
        raise
