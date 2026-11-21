"""Build a Python workflow bundle from an SDK checkout and locked local dependencies."""

import argparse
import ast
import hashlib
import importlib.metadata
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

COMPONENTIZER_VERSION = "0.25.1"
SDK_ROOT = Path(__file__).resolve().parents[1]
# Match the shared modules recognized by the pinned componentizer.
_EXTENSION_SUFFIXES = (".cpython-314-wasm32-wasi.so", ".abi3.so")
_ADDRESSABLE_HASH_DOMAIN = b"temporal-python-cpython-3.14-v1\0"


@dataclass(frozen=True)
class _AddressableFunction:
    slot: str
    function: ast.AsyncFunctionDef
    module: ast.Module

    def stub(self) -> bytes:
        function = self.function
        argument = function.args.args[0].arg
        return (
            "from temporalio._wasm_addressable import invoke as _invoke\n\n"
            f"async def {function.name}({argument}: int) -> int:\n"
            f"    return await _invoke({self.slot!r}, {argument})\n"
        ).encode()


def _addressable_functions(
    source: Path, manifest: Path
) -> dict[str, _AddressableFunction]:
    entries = json.loads(manifest.read_text())
    if not isinstance(entries, dict) or not 1 <= len(entries) <= 1024:
        raise ValueError(
            "Addressable manifest must map slots to module:function entries"
        )
    functions = {}
    for slot, entry in entries.items():
        if not slot or len(slot.encode()) > 128 or not isinstance(entry, str):
            raise ValueError(f"Invalid addressable entry {slot!r}")
        module, _, name = entry.partition(":")
        if (
            not name.isidentifier()
            or not all(part.isidentifier() for part in module.split("."))
            or module.split(".")[0] == "temporalio"
        ):
            raise ValueError(f"Invalid addressable function {entry!r}")
        filename = module.replace(".", "/") + ".py"
        if filename in functions:
            raise ValueError(
                f"Use one addressable function per source module: {module}"
            )
        path = source / filename
        if not path.resolve().is_relative_to(source.resolve()) or path.is_symlink():
            raise ValueError(
                f"Addressable source must be inside the bundle: {filename}"
            )
        tree = ast.parse(path.read_bytes(), filename=filename)
        definitions = [
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name
        ]
        if len(definitions) != 1:
            raise ValueError(f"Expected one top-level async definition for {entry!r}")
        function = definitions[0]
        args = function.args
        if (
            function.decorator_list
            or getattr(function, "type_params", [])
            or len(args.args) != 1
            or args.posonlyargs
            or args.vararg
            or args.kwarg
            or args.kwonlyargs
            or args.defaults
            or not isinstance(args.args[0].annotation, ast.Name)
            or args.args[0].annotation.id != "int"
            or not isinstance(function.returns, ast.Name)
            or function.returns.id != "int"
        ):
            raise ValueError(
                f"Expected an undecorated async (int) -> int function for {entry!r}"
            )
        functions[filename] = _AddressableFunction(slot, function, tree)
    return functions


def _uleb128(value: int) -> bytes:
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def publish_addressable(source: Path, manifest: Path, store: Path) -> None:
    """Publish immutable Python source capsules and replace the live slot map."""
    functions = _addressable_functions(source, manifest)
    store.mkdir(parents=True, exist_ok=True)
    bindings = {}
    # Publish the binding map only after every referenced blob is available.
    with tempfile.TemporaryDirectory(prefix=".publish-", dir=store) as tmp:
        staging = Path(tmp)
        for definition in functions.values():
            source_text = (
                ast.unparse(definition.module)
                + f"\n_temporal_addressable_entry = {definition.function.name}\n"
            ).encode()
            # One passive data segment in a core Wasm module.
            data = b"\x01\x01" + _uleb128(len(source_text)) + source_text
            capsule = b"\0asm\1\0\0\0\x0b" + _uleb128(len(data)) + data
            if len(capsule) > 64 * 1024:
                raise ValueError(
                    f"Addressable capsule exceeds 64 KiB: {definition.slot}"
                )
            digest = hashlib.sha256(_ADDRESSABLE_HASH_DOMAIN + capsule).hexdigest()
            name = f"{digest}.wasm"
            destination = store / name
            if destination.exists():
                if destination.read_bytes() != capsule:
                    raise ValueError(f"Existing WASM capsule is corrupt: {digest}")
            else:
                (staging / name).write_bytes(capsule)
                (staging / name).replace(destination)
            bindings[definition.slot] = digest
        published = staging / "bindings.json"
        published.write_text(json.dumps(bindings, indent=2, sort_keys=True) + "\n")
        published.replace(store / published.name)


def _files(root: Path, pattern: str) -> Iterable[tuple[str, Path]]:
    for path in sorted(root.rglob(pattern)):
        relative = path.relative_to(root)
        if (
            not any(part.startswith(".") for part in relative.parts)
            and not path.is_symlink()
            and path.is_file()
        ):
            yield relative.as_posix(), path


def build(
    output: Path,
    workflows: list[str],
    source: Path,
    dependencies: list[Path],
    core: Path,
    *,
    data_converter: str = "default",
    addressable_manifest: Path | None = None,
) -> None:
    """Package source without importing the workflow modules on the build host."""
    if data_converter not in {"default", "pydantic"}:
        raise ValueError(f"Unknown WASM data converter {data_converter!r}")
    for root in [source, *dependencies]:
        if not root.is_dir():
            raise ValueError(f"Bundle input must be a directory: {root}")
    addressable = (
        _addressable_functions(source, addressable_manifest)
        if addressable_manifest
        else {}
    )
    version = importlib.metadata.version("componentize-py")
    if version != COMPONENTIZER_VERSION:
        raise ValueError(
            f"Expected componentize-py {COMPONENTIZER_VERSION}, found {version}"
        )
    entries = []
    for entry in workflows:
        module, _, class_name = entry.partition(":")
        if not all(
            part.isidentifier()
            for name in (module, class_name)
            for part in name.split(".")
        ):
            raise ValueError("Workflow entries must have the form module:Class")
        entries.append((module, class_name))
    version_assignment = next(
        node
        for node in ast.parse((SDK_ROOT / "temporalio/service.py").read_bytes()).body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        )
    )
    sdk_version = ast.literal_eval(version_assignment.value)
    with tempfile.TemporaryDirectory(prefix="temporal-workflow-wasm-") as tmp:
        stage = Path(tmp)
        trusted = stage / "trusted"
        staged_dependencies = stage / "dependencies"
        for name, path in _files(SDK_ROOT / "temporalio", "*.py"):
            if name.startswith("bridge/sdk-core/"):
                continue
            dest = trusted / "temporalio" / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
        # The guest imports only the workflow runtime from worker, and never needs
        # the service client or its native extension to discover the SDK version.
        (trusted / "temporalio/__init__.py").write_text(
            f"__version__ = {sdk_version!r}\n"
        )
        (trusted / "temporalio/worker/__init__.py").write_text("")
        extensions: dict[str, str] = {}
        for dependency in dependencies:
            for name, path in _files(dependency, "*.py"):
                dest = staged_dependencies / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dest)
            for name, path in _files(dependency, "*.so"):
                if name.startswith("temporalio/") or not name.endswith(
                    _EXTENSION_SUFFIXES
                ):
                    continue
                with path.open("rb") as extension:
                    if extension.read(8) != b"\0asm\1\0\0\0":
                        continue
                dest = staged_dependencies / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dest)
                # The componentizer's linker names libraries by their -p mount index.
                extensions[name] = f"/1/{name}"

        archive_file = io.BytesIO()
        with zipfile.ZipFile(archive_file, "w", zipfile.ZIP_DEFLATED) as archive:
            # SDK and generated protobuf modules can also be imported lazily.
            for root in (staged_dependencies, trusted):
                for name, path in _files(root, "*.py"):
                    archive.writestr(name, path.read_bytes())
            for name, path in _files(source, "*.py"):
                if name.startswith("temporalio/"):
                    raise ValueError(
                        "Workflow source cannot replace the Temporal guest SDK"
                    )
                archive.writestr(
                    name,
                    addressable[name].stub()
                    if name in addressable
                    else path.read_bytes(),
                )
        if (
            not (staged_dependencies / "google/protobuf").is_dir()
            or not (staged_dependencies / "nexusrpc").is_dir()
            or not (staged_dependencies / "typing_extensions.py").is_file()
        ):
            raise ValueError(
                "Dependency paths must include protobuf, nexus-rpc, and typing-extensions"
            )
        (trusted / "temporal_wasm_bundle_data.py").write_text(
            f"ARCHIVE = {archive_file.getvalue()!r}\nWORKFLOWS = {entries!r}\n"
            f"DATA_CONVERTER = {data_converter!r}\n"
            f"EXTENSIONS = {extensions!r}\n"
        )
        shutil.copyfile(
            SDK_ROOT / "scripts/wasm/guest.py", trusted / "temporal_wasm_guest.py"
        )
        env = os.environ.copy()
        env.pop("VIRTUAL_ENV", None)
        env.pop("PYTHONPATH", None)
        subprocess.run(
            [
                str(Path(sys.executable).with_name("componentize-py")),
                "-d",
                str(core / "crates/workflow-wasm/wit"),
                "-w",
                "addressable-workflow-module"
                if addressable
                else "activation-workflow-module",
                "componentize",
                "--stub-wasi",
                "-p",
                str(trusted),
                "-p",
                str(staged_dependencies),
                "temporal_wasm_guest",
                "-o",
                str(output.resolve()),
            ],
            cwd=trusted,
            env=env,
            check=True,
        )
    if data_converter != "default":
        # A top-level custom section keeps the converter profile with the component.
        # Both lengths fit in one unsigned LEB128 byte for these fixed profiles.
        section_name = b"temporal-python-data-converter"
        section = bytes([len(section_name)]) + section_name + data_converter.encode()
        with output.open("ab") as component:
            component.write(bytes([0, len(section)]) + section)
    output.with_suffix(output.suffix + ".json").write_text(
        json.dumps(
            {
                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "componentize-py": version,
                "guest-python": "3.14",
                "sdk-python": sdk_version,
                "data-converter": data_converter,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--output", type=Path)
    action.add_argument("--publish-addressables", type=Path, metavar="STORE")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--workflow", action="append")
    parser.add_argument("--dependency-path", type=Path, action="append")
    parser.add_argument("--addressable-manifest", type=Path)
    parser.add_argument(
        "--data-converter", choices=("default", "pydantic"), default="default"
    )
    parser.add_argument(
        "--core", type=Path, default=SDK_ROOT / "temporalio/bridge/sdk-core"
    )
    args = parser.parse_args()
    if args.publish_addressables is not None:
        if args.addressable_manifest is None:
            parser.error("--publish-addressables requires --addressable-manifest")
        publish_addressable(
            args.source, args.addressable_manifest, args.publish_addressables
        )
        sys.exit(0)
    if not args.workflow or not args.dependency_path:
        parser.error("--output requires --workflow and --dependency-path")
    build(
        args.output,
        args.workflow,
        args.source.resolve(),
        [path.resolve() for path in args.dependency_path],
        args.core.resolve(),
        data_converter=args.data_converter,
        addressable_manifest=args.addressable_manifest,
    )
