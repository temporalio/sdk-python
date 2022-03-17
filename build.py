"""Additional setup options for Poetry build."""

import shutil

from setuptools_rust import Binding, RustExtension


def build(setup_kwargs):
    """Additional setup options for Poetry build."""
    setup_kwargs.update(
        # Same as in scripts/setup_bridge.py, but we cannot import that here
        # because it's not in the sdist
        rust_extensions=[
            RustExtension(
                "temporalio.bridge.temporal_sdk_bridge",
                path="temporalio/bridge/Cargo.toml",
                binding=Binding.PyO3,
                py_limited_api=True,
                features=["pyo3/abi3-py37"],
            )
        ],
        zip_safe=False,
        # We have to remove packages and package data due to duplicate files
        # being generated in the wheel
        packages=[],
        package_data={},
    )
    shutil.rmtree("temporalio.egg-info", ignore_errors=True)
