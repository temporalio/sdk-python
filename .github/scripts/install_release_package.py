"""Install a release package for smoke testing."""

from __future__ import annotations

import argparse
import importlib.metadata
import subprocess
import sys
from collections.abc import Sequence


def _pip_install(args: Sequence[str]) -> None:
    subprocess.check_call([sys.executable, "-m", "pip", "install", *args])


def install_package(args: argparse.Namespace) -> None:
    package = f"temporalio=={args.version}"
    if args.dependency_index_url:
        _pip_install(
            [
                "--prefer-binary",
                "--index-url",
                args.index_url,
                "--no-deps",
                package,
            ]
        )

        requirements = importlib.metadata.requires("temporalio") or []
        if requirements:
            _pip_install(
                [
                    "--prefer-binary",
                    "--index-url",
                    args.dependency_index_url,
                    *requirements,
                ]
            )
    else:
        _pip_install(["--prefer-binary", "--index-url", args.index_url, package])

    subprocess.check_call([sys.executable, "-m", "pip", "check"])


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--index-url", required=True)
    parser.add_argument("--dependency-index-url")
    install_package(parser.parse_args(argv))


if __name__ == "__main__":
    main()
