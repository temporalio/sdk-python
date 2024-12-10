import os
import shutil

from setuptools import setup
from setuptools_rust import Binding, RustExtension

# Same as in /build.py, but we want this self-contained
rust_extensions = [
    RustExtension(
        "temporalio.bridge.temporal_sdk_bridge",
        path="temporalio/bridge/Cargo.toml",
        binding=Binding.PyO3,
        py_limited_api=True,
        features=["pyo3/abi3-py39"],
        # Allow local release builds if requested
        debug=False if os.environ.get("TEMPORAL_BUILD_RELEASE") == "1" else None,
    )
]

if __name__ == "__main__":
    setup(
        name="temporalio",
        version="1.0",
        rust_extensions=rust_extensions,
        zip_safe=False,
    )
    shutil.rmtree("temporalio.egg-info", ignore_errors=True)
