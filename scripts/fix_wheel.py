import os
import sys
import zipfile
from glob import glob

if __name__ == "__main__":
    # Due to https://github.com/python-poetry/poetry/issues/3509 and Poetry
    # assuming the tags, we have to change the wheel ourselves after build

    # Get the file from the dist dir
    dist_files = glob("dist/*.whl")
    if len(dist_files) != 1:
        raise RuntimeError("Should have single wheel file in dist")
    existing_wheel_file = dist_files[0]

    # Rename the wheel file and confirm it is changed. We need to make it py37
    # minimum interpreter and abi3 compat.
    wheel_file_pieces = existing_wheel_file.split("-")
    if len(wheel_file_pieces) < 4:
        raise RuntimeError("Expecting at least 4 wheel pieces")
    wheel_file_pieces[2] = "cp37"
    wheel_file_pieces[3] = "abi3"
    new_wheel_file = "-".join(wheel_file_pieces)
    if existing_wheel_file == new_wheel_file:
        raise RuntimeError("Wheel file already fixed")
    print(f"Converting from {existing_wheel_file} to {new_wheel_file}", file=sys.stderr)

    # Walk the Zip writing files except the WHEEL file which we must alter the
    # tag on the WHEEL file
    with zipfile.ZipFile(new_wheel_file, "w") as zipwrite, zipfile.ZipFile(
        existing_wheel_file, "r"
    ) as zipread:
        found_wheel_tag = False
        for item in zipread.infolist():
            data = zipread.read(item.filename)
            _, _, filename = item.filename.rpartition("/")
            # Change the WHEEL tag
            if filename == "WHEEL":
                lines = data.splitlines()
                for i in range(len(lines)):
                    if lines[i].startswith(b"Tag: "):
                        pieces = lines[i][len("Tag: ") :].split(b"-")
                        if len(pieces) < 3:
                            raise RuntimeError("Expecting at least 3 wheel tag pieces")
                        pieces[0] = b"cp37"
                        pieces[1] = b"abi3"
                        lines[i] = b"Tag: " + b"-".join(pieces)
                        found_wheel_tag = True
                data = b"\n".join(lines)
            zipwrite.writestr(item, data)
        if not found_wheel_tag:
            raise RuntimeError("Did not find WHEEL file with tag entry")
    os.remove(existing_wheel_file)
