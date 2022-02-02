import subprocess
import sys
from pathlib import Path

base_dir = Path(__file__).parent.parent

if __name__ == "__main__":
    print("Building Core bridge", file=sys.stderr)
    subprocess.check_call(
        ["maturin", "develop"], cwd=str(base_dir / "temporalio" / "bridge")
    )
    print("Done", file=sys.stderr)
