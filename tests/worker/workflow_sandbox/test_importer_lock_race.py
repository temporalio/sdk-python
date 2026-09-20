"""Regression tests for the sandbox importer's module-lock re-entry (#1833 evidence).

Two reproductions of the ``KeyError: <thread id>`` from
``<frozen importlib._bootstrap>`` behind the Python 3.10 ``Failed validating
workflow`` flake (#585). Neither depends on the fix's new symbols, so the file
runs against ``main``. Expected: both fail on ``main`` under Python 3.10 and 3.11
and both pass with #1833. Both skip on 3.12+, where CPython fixed its side
(python/cpython#91351) and importlib no longer locks for loaded modules, so
there is nothing left to reproduce.
"""

import gc
import sys
from typing import Any

import pytest

from temporalio.worker.workflow_sandbox._importer import Importer
from temporalio.worker.workflow_sandbox._restrictions import (
    RestrictionContext,
    SandboxRestrictions,
)


@pytest.mark.skipif(
    sys.version_info >= (3, 12),
    reason="importlib's module lock is re-entrant from 3.12 (python/cpython#91351)",
)
def test_workflow_sandbox_importer_loaded_module_import_inside_module_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
):
    # Regression for the 3.10/3.11 failure behind "Failed validating workflow":
    # while the sandbox loads a workflow module, importlib._bootstrap._ModuleLock
    # .acquire holds _blocking_on[tid] until its finally. A GC finalizer inside
    # that window imports `warnings` through builtins.__import__ (the sandbox's);
    # routing that already-loaded module through importlib re-enters the lock and
    # the outer acquire fails with KeyError(<thread id>). Reproduced here
    # deterministically by rebuilding acquire from the interpreter's own
    # _bootstrap.py with the nested import placed inside the window.
    import ast
    import builtins
    import importlib
    import importlib._bootstrap as bootstrap
    import pathlib
    import textwrap

    text = pathlib.Path(importlib.__file__).with_name("_bootstrap.py").read_text()
    acquire_src = next(
        ast.get_source_segment(text, item)
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.ClassDef) and node.name == "_ModuleLock"
        for item in node.body
        if isinstance(item, ast.FunctionDef) and item.name == "acquire"
    )
    assert acquire_src is not None
    marker = "_blocking_on[tid] = self\n"
    assert marker in acquire_src

    nested_done = False

    def nested_import() -> None:
        nonlocal nested_done
        if not nested_done:
            nested_done = True
            builtins.__import__("warnings", {"__builtins__": builtins}, {}, [], 0)

    namespace = dict(vars(bootstrap))
    namespace["_test_nested_import"] = nested_import
    exec(
        textwrap.dedent(acquire_src).replace(
            marker, marker + "        _test_nested_import()\n", 1
        ),
        namespace,
    )
    # Not in typeshed, so reach it dynamically.
    module_lock = getattr(bootstrap, "_ModuleLock")
    monkeypatch.setattr(module_lock, "acquire", namespace["acquire"])

    # A module the sandbox has to load itself (not passthrough), so the outer
    # import really holds importlib's lock with the sandbox importer applied.
    (tmp_path / "sandbox_lock_reentry_module.py").write_text("VALUE = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    # Production restrictions: stdlib (including warnings) is passthrough.
    with Importer(SandboxRestrictions.default, RestrictionContext()).applied():
        import sandbox_lock_reentry_module  # type: ignore[import-not-found]

        assert sandbox_lock_reentry_module.VALUE == 1
    assert nested_done


@pytest.mark.skipif(
    sys.version_info >= (3, 12),
    reason="importlib's module lock is re-entrant from 3.12 (python/cpython#91351)",
)
def test_workflow_sandbox_importer_survives_gc_finalizer_imports():
    # The same failure without touching CPython: keep a garbage cycle whose
    # finalizer imports the already-loaded `warnings` module (what an
    # un-awaited-coroutine RuntimeWarning does) perpetually pending, and make
    # the collector run at nearly every allocation. On 3.10/3.11 the finalizer
    # then lands inside _ModuleLock.acquire's window for imports the sandbox
    # routes through importlib, and the outer import raises KeyError(<tid>).
    # Imports go through builtins.__import__, i.e. the sandbox's importer while
    # it is applied, exactly as an `import` statement would. (3.13+ scales the
    # young-collection threshold to the live heap, so this storm cannot be
    # relied on there; those versions have nothing to reproduce anyway.)
    import builtins

    assert "asyncio" in sys.modules and "warnings" in sys.modules

    state = {"in_finalizer": False, "stop": False, "finalizer_imports": 0}

    class Replenisher:
        def __init__(self) -> None:
            self.me = self  # a cycle: only the cyclic collector frees it

        def __del__(self) -> None:
            if state["in_finalizer"] or state["stop"] or sys.is_finalizing():
                return
            state["in_finalizer"] = True
            try:
                builtins.__import__("warnings", {"__builtins__": builtins}, {}, [], 0)
                state["finalizer_imports"] += 1
                Replenisher()
            finally:
                state["in_finalizer"] = False

    old = gc.get_threshold()
    failures = 0
    imports = 0
    with Importer(SandboxRestrictions.default, RestrictionContext()).applied():
        gc.set_threshold(1)
        Replenisher()
        try:
            # Keep importing until the finalizer has fired a few dozen times,
            # bounded so this stays fast. The list allocations guarantee the
            # collector gets a chance every iteration even on a version whose
            # import path allocates nothing GC-tracked for a loaded module.
            while state["finalizer_imports"] < 25 and imports < 5000:
                imports += 1
                tracked = [[index] for index in range(4)]
                try:
                    builtins.__import__(
                        "asyncio", {"__builtins__": builtins}, {}, [], 0
                    )
                except KeyError:
                    failures += 1
                del tracked
        finally:
            gc.set_threshold(*old)
            state["stop"] = True
            gc.collect()
    assert state["finalizer_imports"] >= 25, (
        f"the finalizer ran only {state['finalizer_imports']} times in {imports} imports"
    )
    assert failures == 0, (
        f"{failures}/{imports} imports raised KeyError inside importlib"
    )
