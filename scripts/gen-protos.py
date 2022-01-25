#!/usr/bin/env python3
import sys
import subprocess
import re
from functools import partial
from pathlib import Path

base_dir = Path(__file__).parent.parent
codegen_dir = base_dir / "temporalio" / "proto"
proto_dir = base_dir / "temporalio" / "bridge" / "sdk-core" / "protos"
api_proto_dir = proto_dir / "api_upstream"
core_proto_dir = proto_dir / "local"
proto_paths = proto_dir.glob("**/*.proto")

fix_import = partial(
    re.compile(r"^from (temporal|dependencies)\.").sub, r"from temporalio.proto.\1."
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
                fixed_content = "".join(fix_import(l) for l in f)
            with p.open("w") as f:
                f.write(fixed_content)


if __name__ == "__main__":
    print("Generating protos...", file=sys.stderr)
    subprocess.check_call(
        [
            sys.executable,
            "-mgrpc_tools.protoc",
            f"--proto_path={api_proto_dir}",
            f"--proto_path={core_proto_dir}",
            f"--python_out={codegen_dir}",
            f"--grpc_python_out={codegen_dir}",
            *map(str, proto_paths),
        ]
    )
    fix_generated_output(codegen_dir)
    print("Done", file=sys.stderr)
