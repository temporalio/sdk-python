"""Install a release package for smoke testing."""

from __future__ import annotations

import argparse
import importlib.metadata
import subprocess
import sys
import time
from collections.abc import Sequence

RELEASE_INSTALL_ATTEMPTS = 31
RELEASE_INSTALL_RETRY_SECONDS = 30


def _pip_install(args: Sequence[str]) -> None:
    subprocess.check_call([sys.executable, "-m", "pip", "install", *args])


def _pip_install_with_retries(args: Sequence[str]) -> None:
    for attempt in range(1, RELEASE_INSTALL_ATTEMPTS + 1):
        try:
            _pip_install(args)
            return
        except subprocess.CalledProcessError:
            if attempt == RELEASE_INSTALL_ATTEMPTS:
                raise
            print(
                "Package was not installable yet; retrying in "
                f"{RELEASE_INSTALL_RETRY_SECONDS}s "
                f"({attempt}/{RELEASE_INSTALL_ATTEMPTS})",
                flush=True,
            )
            time.sleep(RELEASE_INSTALL_RETRY_SECONDS)


def install_package(args: argparse.Namespace) -> None:
    package = f"temporalio=={args.version}"
    if args.dependency_index_url:
        _pip_install_with_retries(
            [
                "--prefer-binary",
                "--no-cache-dir",
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
        _pip_install_with_retries(
            [
                "--prefer-binary",
                "--no-cache-dir",
                "--index-url",
                args.index_url,
                package,
            ]
        )

    subprocess.check_call([sys.executable, "-m", "pip", "check"])


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--index-url", required=True)
    parser.add_argument("--dependency-index-url")
    install_package(parser.parse_args(argv))


if __name__ == "__main__":
    main()
