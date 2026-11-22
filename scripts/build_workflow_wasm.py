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
import symtable
import sys
import tempfile
import tokenize
import zipfile
from collections import deque
from collections.abc import Iterable
from copy import copy
from dataclasses import dataclass, field
from importlib.util import resolve_name
from pathlib import Path

COMPONENTIZER_VERSION = "0.25.1"
SDK_ROOT = Path(__file__).resolve().parents[1]
# Match the shared modules recognized by the pinned componentizer.
_EXTENSION_SUFFIXES = (".cpython-314-wasm32-wasi.so", ".abi3.so")
_ADDRESSABLE_HASH_DOMAIN = b"temporal-python-cpython-3.14-v1\0"


@dataclass(frozen=True)
class _AddressableFunction:
    slot: str
    function: ast.FunctionDef | ast.AsyncFunctionDef
    module: ast.Module


_AddressableFunctions = dict[tuple[str, str], _AddressableFunction]


@dataclass(frozen=True)
class _SourceModule:
    filename: str
    tree: ast.Module
    scope: symtable.SymbolTable
    bindings: dict[str, str]


@dataclass
class _SourceCallGraph:
    definitions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = field(
        default_factory=dict
    )
    calls: dict[str, list[str]] = field(default_factory=dict)


def _addressable_modules(functions: _AddressableFunctions) -> dict[str, bytes]:
    by_file: dict[str, list[_AddressableFunction]] = {}
    for (filename, _), definition in functions.items():
        by_file.setdefault(filename, []).append(definition)
    sources = {}
    for filename, definitions in by_file.items():
        tree = definitions[0].module
        alias = "_temporal_addressable_bind"
        original = ast.unparse(tree)
        while alias in original:
            alias += "_"
        replacements: dict[ast.stmt, ast.stmt] = {}
        for definition in definitions:
            function = copy(definition.function)
            decorator = ast.parse(
                f"{alias}({definition.slot!r}, __name__, __package__)", mode="eval"
            ).body
            function.decorator_list = [decorator, *function.decorator_list]
            replacements[definition.function] = function
        body = [replacements.get(node, node) for node in tree.body]
        # Python requires future imports to precede the generated import.
        position = int(ast.get_docstring(tree) is not None)
        while (
            position < len(body)
            and isinstance(node := body[position], ast.ImportFrom)
            and node.module == "__future__"
        ):
            position += 1
        body[position:position] = ast.parse(
            f"from temporalio._wasm_addressable import bind as {alias}"
        ).body
        sources[filename] = ast.unparse(
            ast.Module(body=body, type_ignores=tree.type_ignores)
        ).encode()
    return sources


def _addressable_definition(
    module: ast.Module, name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    definitions = [
        node
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    return definitions[0] if len(definitions) == 1 else None


def _module_imports(
    tree: ast.Module, scope: symtable.SymbolTable, package: str
) -> dict[str, str]:
    imports: dict[str, str] = {}
    roots: dict[str, str] = {}
    ambiguous: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            bindings = [
                (alias.asname or alias.name, alias.name) for alias in node.names
            ]
        elif isinstance(node, ast.ImportFrom):
            try:
                imported_module = resolve_name(
                    "." * node.level + (node.module or ""), package
                )
            except ImportError:
                continue
            bindings = [
                (alias.asname or alias.name, f"{imported_module}.{alias.name}")
                for alias in node.names
                if alias.name != "*"
            ]
        else:
            continue
        for local, imported in bindings:
            root = local.partition(".")[0]
            # Both `import pkg.sub` and `import pkg.other` bind the same root package.
            binding = imported if root == local else root
            if (
                roots.setdefault(root, binding) != binding
                or scope.lookup(root).is_assigned()
            ):
                ambiguous.add(root)
            imports[local] = imported
    return {
        local: target
        for local, target in imports.items()
        if local.partition(".")[0] not in ambiguous
    }


def _module_calls(
    nodes: Iterable[ast.AST], scope: symtable.SymbolTable, bindings: dict[str, str]
) -> Iterable[str]:
    for node in nodes:
        # Calls in nested scopes can use a different binding for the same name.
        if isinstance(
            node,
            (
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.Lambda,
                ast.ListComp,
                ast.SetComp,
                ast.DictComp,
                ast.GeneratorExp,
            ),
        ):
            continue
        # Calls used as arguments run before their enclosing call.
        yield from _module_calls(ast.iter_child_nodes(node), scope, bindings)
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        attributes = []
        while isinstance(target, ast.Attribute):
            attributes.append(target.attr)
            target = target.value
        if not isinstance(target, ast.Name) or not scope.lookup(target.id).is_global():
            continue
        path = [target.id, *reversed(attributes)]
        for size in range(1, len(path) + 1):
            imported = bindings.get(".".join(path[:size]))
            if imported is not None:
                yield ".".join([imported, *path[size:]])
                break


def _definition_scope(
    parent: symtable.SymbolTable,
    definition: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
) -> symtable.SymbolTable:
    scope = next(
        child
        for child in parent.get_children()
        if child.get_name() == definition.name
        and child.get_lineno() == definition.lineno
    )
    if getattr(definition, "type_params", []):
        # Python wraps a generic definition in a separate type-parameter scope.
        return next(
            child
            for child in scope.get_children()
            if child.get_name() == definition.name
        )
    return scope


def _discover_addressable_functions(
    source: Path,
    workflows: list[tuple[str, str]],
    *,
    call_graph: _SourceCallGraph | None = None,
) -> _AddressableFunctions:
    files = dict(_files(source, "*.py"))
    modules: dict[str, _SourceModule] = {}

    def load_module(module: str) -> _SourceModule | None:
        if module in modules:
            return modules[module]
        module_path = module.replace(".", "/")
        filename = f"{module_path}.py"
        package = module.rpartition(".")[0]
        if filename not in files:
            filename = f"{module_path}/__init__.py"
            package = module
        if filename not in files:
            return None
        with tokenize.open(files[filename]) as file:
            module_source = file.read()
        tree = ast.parse(module_source, filename=filename)
        scope = symtable.symtable(module_source, filename, "exec")
        bindings = _module_imports(tree, scope, package)
        bindings.update(
            (node.name, f"{module}.{node.name}")
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not scope.lookup(node.name).is_imported()
        )
        parsed = _SourceModule(filename, tree, scope, bindings)
        modules[module] = parsed
        return parsed

    pending: deque[tuple[str, str | None, str]] = deque()
    for module, class_name in workflows:
        parsed = load_module(module)
        if parsed is None:
            raise ValueError(f"Workflow source module must be in --source: {module}")
        body = parsed.tree.body
        scope = parsed.scope
        for part in class_name.split("."):
            klass = next(
                (
                    node
                    for node in body
                    if isinstance(node, ast.ClassDef) and node.name == part
                ),
                None,
            )
            if klass is None:
                raise ValueError(
                    f"Expected a workflow class definition for {module}:{class_name}"
                )
            scope = _definition_scope(scope, klass)
            body = klass.body
        for method in body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            caller = f"{module}:{class_name}.{method.name}"
            if call_graph is not None:
                call_graph.definitions[caller] = method
            pending.extend(
                (target, None, caller)
                for target in _module_calls(
                    method.body, _definition_scope(scope, method), parsed.bindings
                )
            )

    functions: _AddressableFunctions = {}
    visited: set[str] = set()
    while pending:
        target, caller_module, caller = pending.popleft()
        module, _, name = target.rpartition(".")
        parsed = load_module(module)
        if parsed is None:
            continue
        function = _addressable_definition(parsed.tree, name)
        if function is None:
            continue
        slot = f"{module}:{name}"
        if call_graph is not None:
            call_graph.definitions[slot] = function
            call_graph.calls.setdefault(caller, []).append(slot)
        if caller_module != module:
            if len(slot.encode()) > 128:
                raise ValueError(f"Addressable slot exceeds 128 bytes: {slot}")
            functions[parsed.filename, name] = _AddressableFunction(
                slot, function, parsed.tree
            )
        if target in visited:
            continue
        visited.add(target)
        # Calls in the same module use the caller's capsule. Follow them to
        # reach imports that can dispatch independently from that capsule.
        pending.extend(
            (callee, module, slot)
            for callee in _module_calls(
                function.body,
                _definition_scope(parsed.scope, function),
                parsed.bindings,
            )
        )
    if not functions:
        raise ValueError(
            "No eligible addressable calls found in the selected workflows"
        )
    if len(functions) > 1024:
        raise ValueError("Too many addressable functions in the selected workflows")
    return functions


def _manifest_addressable_functions(
    source: Path, manifest: Path
) -> _AddressableFunctions:
    entries = json.loads(manifest.read_text())
    if not isinstance(entries, dict) or not 1 <= len(entries) <= 1024:
        raise ValueError(
            "Addressable manifest must map slots to module:function entries"
        )
    functions: _AddressableFunctions = {}
    trees: dict[str, ast.Module] = {}
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
        if (filename, name) in functions:
            raise ValueError(f"Use one addressable slot per function: {entry}")
        path = source / filename
        if not path.resolve().is_relative_to(source.resolve()) or path.is_symlink():
            raise ValueError(
                f"Addressable source must be inside the bundle: {filename}"
            )
        if filename not in trees:
            trees[filename] = ast.parse(path.read_bytes(), filename=filename)
        tree = trees[filename]
        function = _addressable_definition(tree, name)
        if function is None:
            raise ValueError(
                f"Expected one top-level function definition for {entry!r}"
            )
        functions[filename, name] = _AddressableFunction(slot, function, tree)
    return functions


def _uleb128(value: int) -> bytes:
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def _workflow_entries(workflows: list[str]) -> list[tuple[str, str]]:
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
    return entries


def publish_addressable(
    source: Path,
    manifest: Path | None,
    store: Path,
    *,
    workflows: list[str] | None = None,
) -> None:
    """Publish immutable Python source capsules and replace the live slot map."""
    functions = (
        _manifest_addressable_functions(source, manifest)
        if manifest is not None
        else _discover_addressable_functions(source, _workflow_entries(workflows or []))
    )
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
    discover_addressables: bool = False,
    addressable_manifest: Path | None = None,
) -> None:
    """Package source without importing the workflow modules on the build host."""
    if data_converter not in {"default", "pydantic"}:
        raise ValueError(f"Unknown WASM data converter {data_converter!r}")
    for root in [source, *dependencies]:
        if not root.is_dir():
            raise ValueError(f"Bundle input must be a directory: {root}")
    entries = _workflow_entries(workflows)
    if addressable_manifest is not None:
        addressable = _addressable_modules(
            _manifest_addressable_functions(source, addressable_manifest)
        )
    elif discover_addressables:
        addressable = _addressable_modules(
            _discover_addressable_functions(source, entries)
        )
    else:
        addressable = {}
    version = importlib.metadata.version("componentize-py")
    if version != COMPONENTIZER_VERSION:
        raise ValueError(
            f"Expected componentize-py {COMPONENTIZER_VERSION}, found {version}"
        )
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
                    addressable[name] if name in addressable else path.read_bytes(),
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
    addressable = parser.add_mutually_exclusive_group()
    addressable.add_argument(
        "--addressable",
        action="store_true",
        help="Discover addressable subroutines in the selected workflows",
    )
    addressable.add_argument("--addressable-manifest", type=Path)
    parser.add_argument(
        "--data-converter", choices=("default", "pydantic"), default="default"
    )
    parser.add_argument(
        "--core", type=Path, default=SDK_ROOT / "temporalio/bridge/sdk-core"
    )
    args = parser.parse_args()
    if args.publish_addressables is not None:
        if args.addressable_manifest is None and not args.workflow:
            parser.error(
                "--publish-addressables requires --workflow or --addressable-manifest"
            )
        publish_addressable(
            args.source,
            args.addressable_manifest,
            args.publish_addressables,
            workflows=args.workflow,
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
        discover_addressables=args.addressable,
        addressable_manifest=args.addressable_manifest,
    )
