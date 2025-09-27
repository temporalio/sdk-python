import shutil
import subprocess
from pathlib import Path

base_dir = Path(__file__).parent.parent

if __name__ == "__main__":
    print("Generating documentation...")

    # Run pydoctor
    subprocess.check_call(["pydoctor", "--quiet"])

    # Copy favicon
    shutil.copyfile(
        base_dir / "scripts" / "_img" / "favicon.ico",
        base_dir / "build" / "apidocs" / "favicon.ico",
    )
