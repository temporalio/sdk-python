#!/usr/bin/env python3
import os
import re
import shutil
import subprocess
import sys
import tempfile
from functools import partial
from pathlib import Path

base_dir = Path(__file__).parent.parent
proto_dir = base_dir / "temporalio" / "bridge" / "sdk-core" / "protos"
api_proto_dir = proto_dir / "api_upstream"
core_proto_dir = proto_dir / "local"
proto_paths = proto_dir.glob("**/*.proto")

api_out_dir = base_dir / "temporalio" / "api"
sdk_out_dir = base_dir / "temporalio" / "bridge" / "proto"

fix_api_import = partial(
    re.compile(r"from temporal\.api\.").sub, r"from temporalio.api."
)
fix_dependency_import = partial(
    re.compile(r"from dependencies\.").sub, r"from temporalio.api.dependencies."
)
fix_sdk_import = partial(
    re.compile(r"from temporal\.sdk\.core\.").sub, r"from temporalio.bridge.proto."
)


def fix_generated_output(base_path: Path):
    """Fix the generated protoc output

    - protoc doesn't generate __init__.py files
    - protoc doesn't generate the correct import paths
        (https://github.com/protocolbuffers/protobuf/issues/1491)
    """

    (base_path / "__init__.py").touch()
    for p in base_path.iterdir():
        if p.is_dir():
            fix_generated_output(p)
        else:
            with p.open(encoding="utf8") as f:
                content = f.read()
                content = fix_api_import(content)
                content = fix_dependency_import(content)
                content = fix_sdk_import(content)
            with p.open("w") as f:
                f.write(content)


if __name__ == "__main__":
    print("Generating protos...", file=sys.stderr)
    with tempfile.TemporaryDirectory(dir=base_dir) as temp_dir:
        temp_dir = Path(temp_dir)
        subprocess.check_call(
            [
                sys.executable,
                "-mgrpc_tools.protoc",
                f"--proto_path={api_proto_dir}",
                f"--proto_path={core_proto_dir}",
                f"--python_out={temp_dir}",
                f"--grpc_python_out={temp_dir}",
                *map(str, proto_paths),
            ]
        )
        # Apply import fixes before moving code
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
    print("Done", file=sys.stderr)
