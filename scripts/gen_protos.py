import collections
import re
import shutil
import subprocess
import sys
import tempfile
from functools import partial
from pathlib import Path
from typing import List, Mapping

base_dir = Path(__file__).parent.parent
proto_dir = base_dir / "temporalio" / "bridge" / "sdk-core" / "protos"
api_proto_dir = proto_dir / "api_upstream"
core_proto_dir = proto_dir / "local"
health_proto_dir = proto_dir / "grpc"
testsrv_proto_dir = proto_dir / "testsrv_upstream"
# Needed for descriptor.proto which was removed as part of
# https://github.com/grpc/grpc/pull/30377
additional_well_known_proto_dir = base_dir / "scripts" / "_proto"

# Exclude testsrv dependencies protos
proto_paths = (
    v
    for v in proto_dir.glob("**/*.proto")
    if not str(v).startswith(str(testsrv_proto_dir / "dependencies"))
)

api_out_dir = base_dir / "temporalio" / "api"
sdk_out_dir = base_dir / "temporalio" / "bridge" / "proto"

py_fixes = [
    partial(re.compile(r"from temporal\.api\.").sub, r"from temporalio.api."),
    partial(
        re.compile(r"from dependencies\.").sub, r"from temporalio.api.dependencies."
    ),
    partial(
        re.compile(r"from temporal\.sdk\.core\.").sub, r"from temporalio.bridge.proto."
    ),
]

pyi_fixes = [
    partial(re.compile(r"temporal\.api\.").sub, r"temporalio.api."),
    partial(re.compile(r"temporal\.sdk\.core\.").sub, r"temporalio.bridge.proto."),
]

find_class_re = re.compile(r"\nclass ([^_\(\:]+)")
find_def_re = re.compile(r"\ndef ([^\(\:]+)")


def fix_generated_output(base_path: Path):
    """Fix the generated protoc output

    - protoc doesn't generate __init__.py files nor re-export the types we want
    - protoc doesn't generate the correct import paths
        (https://github.com/protocolbuffers/protobuf/issues/1491)
    """

    imports: Mapping[str, List[str]] = collections.defaultdict(list)
    for p in base_path.iterdir():
        if p.is_dir():
            fix_generated_output(p)
        elif p.suffix == ".py" or p.suffix == ".pyi":
            with p.open(encoding="utf8") as f:
                content = f.read()
                if p.suffix == ".py":
                    # Defs in .py, classes in .pyi
                    imports[p.stem] += find_def_re.findall(content)
                    for fix in py_fixes:
                        content = fix(content)
                else:
                    imports[p.stem] += find_class_re.findall(content)
                    for fix in pyi_fixes:
                        content = fix(content)
            with p.open("w") as f:
                f.write(content)
    # Write init
    with (base_path / "__init__.py").open("w") as f:
        # Add docstring to API's init
        if str(base_path.as_posix()).endswith("/temporal/api"):
            f.write('"""Temporal API protobuf models."""\n')
        # Non-gRPC imports
        message_names = []
        for stem, messages in imports.items():
            if stem != "service_pb2_grpc":
                for message in messages:
                    f.write(f"from .{stem} import {message}\n")
                    message_names.append(message)
        # __all__
        message_names = sorted(message_names)
        if message_names:
            f.write(
                f'\n__all__ = [\n    "' + '",\n    "'.join(message_names) + '",\n]\n'
            )
        # gRPC imports
        if "service_pb2_grpc" in imports:
            message_names = []
            f.write("\n# gRPC is optional\ntry:\n    import grpc\n")
            for message in imports["service_pb2_grpc"]:
                # MyPy protobuf does not document this experimental class, see
                # https://github.com/nipunn1313/mypy-protobuf/issues/212#issuecomment-885300106
                import_suffix = ""
                if (
                    message == "WorkflowService"
                    or message == "OperatorService"
                    or message == "TestService"
                ):
                    import_suffix = " # type: ignore"
                f.write(f"    from .service_pb2_grpc import {message}{import_suffix}\n")
                message_names.append(message)
            # __all__
            message_names = sorted(message_names)
            f.write(f'    __all__.extend(["' + '", "'.join(message_names) + '"])\n')
            f.write("except ImportError:\n    pass")


if __name__ == "__main__":
    print("Generating protos...", file=sys.stderr)
    with tempfile.TemporaryDirectory(dir=base_dir) as temp_dir_raw:
        temp_dir = Path(temp_dir_raw)
        subprocess.check_call(
            [
                sys.executable,
                "-mgrpc_tools.protoc",
                f"--proto_path={api_proto_dir}",
                f"--proto_path={core_proto_dir}",
                f"--proto_path={testsrv_proto_dir}",
                f"--proto_path={health_proto_dir}",
                f"--proto_path={additional_well_known_proto_dir}",
                f"--python_out={temp_dir}",
                f"--grpc_python_out={temp_dir}",
                f"--mypy_out={temp_dir}",
                f"--mypy_grpc_out={temp_dir}",
                *map(str, proto_paths),
            ]
        )
        # Remove health gRPC parts
        (temp_dir / "health" / "v1" / "health_pb2_grpc.py").unlink()
        (temp_dir / "health" / "v1" / "health_pb2_grpc.pyi").unlink()
        # Apply fixes before moving code
        fix_generated_output(temp_dir)
        # Move protos
        for p in (temp_dir / "temporal" / "api").iterdir():
            shutil.rmtree(api_out_dir / p.name, ignore_errors=True)
            p.replace(api_out_dir / p.name)
        shutil.rmtree(api_out_dir / "dependencies", ignore_errors=True)
        (temp_dir / "dependencies").replace(api_out_dir / "dependencies")
        for p in (temp_dir / "temporal" / "sdk" / "core").iterdir():
            shutil.rmtree(sdk_out_dir / p.name, ignore_errors=True)
            p.replace(sdk_out_dir / p.name)
        shutil.rmtree(sdk_out_dir / "health", ignore_errors=True)
        (temp_dir / "health").replace(sdk_out_dir / "health")
    print("Done", file=sys.stderr)
