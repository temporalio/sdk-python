import collections
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from functools import partial
from pathlib import Path

base_dir = Path(__file__).parent.parent
proto_dir = (
    base_dir / "temporalio" / "bridge" / "sdk-core" / "crates" / "common" / "protos"
)
api_proto_dir = proto_dir / "api_upstream"
api_cloud_proto_dir = proto_dir / "api_cloud_upstream"
core_proto_dir = proto_dir / "local"
testsrv_proto_dir = proto_dir / "testsrv_upstream"
test_proto_dir = base_dir / "tests"
additional_proto_dir = base_dir / "scripts" / "_proto"
health_proto_dir = additional_proto_dir / "grpc"

# Exclude testsrv dependencies protos
proto_paths = [
    v
    for v in proto_dir.glob("**/*.proto")
    if not str(v).startswith(str(testsrv_proto_dir / "dependencies"))
    and not "health" in str(v)
    and not "google" in str(v)
]
proto_paths.extend(test_proto_dir.glob("**/*.proto"))
proto_paths.extend(additional_proto_dir.glob("**/*.proto"))

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
    partial(
        re.compile(r"'__module__' : 'temporal\.api\.").sub,
        r"'__module__' : 'temporalio.api.",
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
    imports: Mapping[str, list[str]] = collections.defaultdict(list)
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


def check_proto_toolchain_versions():
    """
    Check protobuf and grpcio versions.

    Due to issues with the Python protobuf 3.x vs protobuf 4.x libraries, we
    must require that grpcio tools be on 1.48.x and protobuf be on 3.x for
    generation of protos. We can't check __version__ on the module (not
    present), and we can't use importlib.metadata due to its absence in 3.7,
    so we just run pip and check there.
    """
    proc = subprocess.run(
        ["uv", "pip", "list", "--format", "freeze"],
        check=True,
        capture_output=True,
        text=True,
    )
    proto_version = ""
    grpcio_tools_version = ""
    for line in proc.stdout.splitlines():
        if line.startswith("protobuf"):
            _, _, proto_version = line.partition("==")
        elif line.startswith("grpcio-tools"):
            _, _, grpcio_tools_version = line.partition("==")
    assert proto_version.startswith(
        "3."
    ), f"expected 3.x protobuf, found {proto_version}"
    assert grpcio_tools_version.startswith(
        "1.48."
    ), f"expected 1.48.x grpcio-tools, found {grpcio_tools_version}"


def generate_protos(output_dir: Path):
    subprocess.check_call(
        [
            sys.executable,
            "-mgrpc_tools.protoc",
            f"--proto_path={api_proto_dir}",
            f"--proto_path={api_cloud_proto_dir}",
            f"--proto_path={core_proto_dir}",
            f"--proto_path={testsrv_proto_dir}",
            f"--proto_path={health_proto_dir}",
            f"--proto_path={test_proto_dir}",
            f"--proto_path={additional_proto_dir}",
            f"--python_out={output_dir}",
            f"--grpc_python_out={output_dir}",
            f"--mypy_out={output_dir}",
            f"--mypy_grpc_out={output_dir}",
            *map(str, proto_paths),
        ]
    )
    # Remove every _grpc.py file that isn't part of a Temporal "service"
    for grpc_file in output_dir.glob("**/*_grpc.py*"):
        if (
            len(grpc_file.parents) < 2
            or grpc_file.parents[0].name != "v1"
            or not grpc_file.parents[1].name.endswith("service")
        ):
            grpc_file.unlink()
    # Apply fixes before moving code
    fix_generated_output(output_dir)
    # Move protos
    for p in (output_dir / "temporal" / "api").iterdir():
        shutil.rmtree(api_out_dir / p.name, ignore_errors=True)
        p.replace(api_out_dir / p.name)
    shutil.rmtree(api_out_dir / "dependencies", ignore_errors=True)
    for p in (output_dir / "temporal" / "sdk" / "core").iterdir():
        shutil.rmtree(sdk_out_dir / p.name, ignore_errors=True)
        p.replace(sdk_out_dir / p.name)
    shutil.rmtree(sdk_out_dir / "health", ignore_errors=True)
    (output_dir / "health").replace(sdk_out_dir / "health")
    # Move test protos
    for v in ["__init__.py", "proto_message_pb2.py", "proto_message_pb2.pyi"]:
        shutil.copy2(
            output_dir / "worker" / "workflow_sandbox" / "testmodules" / "proto" / v,
            test_proto_dir
            / "worker"
            / "workflow_sandbox"
            / "testmodules"
            / "proto"
            / v,
        )


if __name__ == "__main__":
    check_proto_toolchain_versions()
    print("Generating protos...", file=sys.stderr)
    with tempfile.TemporaryDirectory(dir=base_dir) as temp_dir:
        generate_protos(Path(temp_dir))
    print("Done", file=sys.stderr)
