#!/usr/bin/env python3
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
proto_paths = proto_dir.glob("**/*.proto")

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

find_message_re = re.compile(r"_sym_db\.RegisterMessage\(([^\)\.]+)\)")
find_enum_re = re.compile(r"DESCRIPTOR\.enum_types_by_name\['([^']+)'\] =")
find_class_re = re.compile(r"\nclass ([^\(\:]+)")
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
                    for fix in py_fixes:
                        content = fix(content)
                    # Only use .py files to determine imports, not pyi ones
                    imports[p.stem] += find_message_re.findall(content)
                    imports[p.stem] += find_enum_re.findall(content)
                    imports[p.stem] += find_class_re.findall(content)
                    imports[p.stem] += find_def_re.findall(content)
                else:
                    for fix in pyi_fixes:
                        content = fix(content)
            with p.open("w") as f:
                f.write(content)
    # Write init
    with (base_path / "__init__.py").open("w") as f:
        for stem, messages in imports.items():
            for message in messages:
                f.write(f"from .{stem} import {message}\n")


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
                f"--python_out={temp_dir}",
                f"--grpc_python_out={temp_dir}",
                f"--mypy_out={temp_dir}",
                f"--mypy_grpc_out={temp_dir}",
                *map(str, proto_paths),
            ]
        )
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
    print("Done", file=sys.stderr)
