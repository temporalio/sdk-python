"""Setuptools entry points for environments that invoke ``setup.py`` directly.

The canonical build definition remains ``pyproject.toml`` / ``maturin``. This
module delegates ``sdist`` and ``bdist_wheel`` to ``python -m maturin`` so
legacy ``python setup.py sdist`` / ``bdist_wheel`` workflows produce the same
artifacts as a PEP 517 build.

Bootstrap tradeoff: if ``maturin`` is not importable as a module, this script
installs ``maturin`` into the *current* interpreter with ``pip`` (requires
network on cold builders). Builders that pre-install ``maturin`` avoid that
step. Alternatives such as ``setup_requires`` are avoided because they interact
poorly with modern pip and isolated metadata resolution.

``maturin sdist`` / ``maturin build`` still invoke Cargo metadata and require a
Rust toolchain (``cargo`` on ``PATH``), same as a normal PEP 517 build.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from setuptools import setup
from setuptools.command.bdist_wheel import bdist_wheel as _bdist_wheel
from setuptools.command.sdist import sdist as _sdist

ROOT = Path(__file__).resolve().parent
DIST_DIR = "dist"
MATURIN_REQ = "maturin>=1.0,<2.0"


def _output_dir(cmd: object) -> str:
    """Directory for artifacts; honors setuptools ``--dist-dir`` / ``-d`` when set."""
    dist_dir = getattr(cmd, "dist_dir", None)
    if dist_dir:
        return str(dist_dir)
    return DIST_DIR


def _have_maturin() -> bool:
    exe = shutil.which("maturin")
    if exe:
        return True
    try:
        subprocess.run(
            [sys.executable, "-m", "maturin", "--version"],
            check=True,
            capture_output=True,
            cwd=ROOT,
        )
        return True
    except (subprocess.CalledProcessError, OSError):
        return False


def _ensure_maturin() -> None:
    if _have_maturin():
        return
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            MATURIN_REQ,
        ],
        cwd=ROOT,
    )


def _maturin_cmd() -> list[str]:
    _ensure_maturin()
    if shutil.which("maturin"):
        return ["maturin"]
    return [sys.executable, "-m", "maturin"]


def _run_maturin(args: list[str]) -> None:
    cmd = _maturin_cmd() + args
    sys.stderr.write("Running: " + " ".join(cmd) + "\n")
    subprocess.check_call(cmd, cwd=ROOT)


class sdist(_sdist):
    def run(self) -> None:
        out = _output_dir(self)
        self.mkpath(out)
        _run_maturin(["sdist", "-o", out])


class bdist_wheel(_bdist_wheel):
    def run(self) -> None:
        out = _output_dir(self)
        self.mkpath(out)
        _run_maturin(
            [
                "build",
                "--release",
                "-o",
                out,
            ]
        )


setup(
    cmdclass={
        "sdist": sdist,
        "bdist_wheel": bdist_wheel,
    },
)
