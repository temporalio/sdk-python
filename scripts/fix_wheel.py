import os
import pathlib
import shutil
import subprocess
from glob import glob

if __name__ == "__main__":
    # Due to https://github.com/python-poetry/poetry/issues/3509 and Poetry
    # assuming the tags, we have to change the wheel ourselves after build. In
    # order to keep checksums proper, we use wheel pack/unpack.

    # Get the file from the dist dir
    dist_files = glob("dist/*.whl")
    if len(dist_files) != 1:
        raise RuntimeError(f"Should have only one wheel file, found: {dist_files}")

    # Run unpack into temp directory
    if os.path.exists("dist/temp"):
        raise RuntimeError("dist/temp directory already present")
    subprocess.check_call(["wheel", "unpack", "--dest", "dist/temp", dist_files[0]])

    # Read WHEEL contents
    wheel_files = glob("dist/temp/*/*.dist-info/WHEEL")
    if len(wheel_files) != 1:
        raise RuntimeError(f"Should have only one WHEEL file, found: {wheel_files}")
    with open(wheel_files[0]) as f:
        wheel_lines = f.read().splitlines()

    # Alter the "Tag"
    found_wheel_tag = False
    for i, line in enumerate(wheel_lines):
        if line.startswith("Tag: "):
            pieces = line[len("Tag: ") :].split("-")
            if len(pieces) < 3:
                raise RuntimeError("Expecting at least 3 wheel tag pieces")
            # All tags need ABI3
            pieces[1] = "abi3"
            if pieces[2].startswith("macosx_") and pieces[2].endswith("_arm64"):
                # macOS ARM needs to be set to 3.8+ on 11+
                pieces[0] = "cp38"
                pieces[2] = "macosx_11_0_arm64"
            elif pieces[2].startswith("macosx_") and pieces[2].endswith("_x86_64"):
                # macOS x86 needs to be set to 3.8+ on 10.9+
                pieces[0] = "cp38"
                pieces[2] = "macosx_10_9_x86_64"
            else:
                # All others just need to be set to 3.8+
                pieces[0] = "cp38"
            wheel_lines[i] = "Tag: " + "-".join(pieces)
            found_wheel_tag = True
    if not found_wheel_tag:
        raise RuntimeError("Could not find WHEEL tag")

    # On Windows due to setuptools-rust changing how the pyd file is named, we
    # are renaming it back.
    # https://github.com/PyO3/setuptools-rust/pull/352#discussion_r1293444464
    # explains our exact situation, but no clear remedy.
    # TODO(cretz): Investigate as part of https://github.com/temporalio/sdk-python/issues/398
    pyd_files = glob("dist/temp/*/temporalio/bridge/*-win_amd64.pyd")
    if pyd_files:
        os.rename(
            pyd_files[0],
            pathlib.Path(pyd_files[0]).with_name("temporal_sdk_bridge.pyd"),
        )

    # Write the WHEEL file
    with open(wheel_files[0], "w") as f:
        f.write("\n".join(wheel_lines))

    # Pack the wheel
    unpacked_dirs = glob("dist/temp/*")
    subprocess.check_call(["wheel", "pack", "--dest", "dist", unpacked_dirs[0]])

    # Remove temp dir
    shutil.rmtree("dist/temp")

    # If there are multiple wheels now, remove the old one
    new_dist_files = glob("dist/*.whl")
    new_dist_files.remove(dist_files[0])
    if new_dist_files:
        os.remove(dist_files[0])
        print(f"Created wheel {new_dist_files[0]} from {dist_files[0]}")
    else:
        print(f"Overwrote wheel {dist_files[0]}")
