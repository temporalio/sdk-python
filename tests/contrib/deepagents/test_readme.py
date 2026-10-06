"""The README's code blocks must stay valid Python.

A copy-paste example that does not even parse is worse than no example. This
compiles every ```python fenced block in the README so a stale snippet fails the
suite. Compilation (not execution) keeps the check dependency-free.
"""

from __future__ import annotations

import re
from pathlib import Path

README = (
    Path(__file__).resolve().parents[3]
    / "temporalio"
    / "contrib"
    / "deepagents"
    / "README.md"
)

_BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)


def _python_blocks() -> list[str]:
    return _BLOCK.findall(README.read_text(encoding="utf-8"))


def test_readme_has_python_blocks() -> None:
    blocks = _python_blocks()
    assert len(blocks) >= 3, "expected the hello-world and composition examples"


def test_readme_hello_world_constructs() -> None:
    # Every documented snippet must compile as written.
    for i, block in enumerate(_python_blocks()):
        compile(block, f"<README block {i}>", "exec")
