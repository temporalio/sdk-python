import dataclasses
import importlib
import importlib.machinery
import sys
import types
from typing import Any

import pytest

from temporalio import workflow
from temporalio.worker.workflow_sandbox._importer import (
    Importer,
    _loaded_module_for_import,
    _thread_local_sys_modules,
    _ThreadLocalSysModules,
)
from temporalio.worker.workflow_sandbox._restrictions import (
    RestrictedWorkflowAccessError,
    RestrictionContext,
    SandboxRestrictions,
)

from .testmodules import restrictions


def test_workflow_sandbox_importer_invalid_module():
    with pytest.raises(RestrictedWorkflowAccessError) as err:
        with Importer(restrictions, RestrictionContext()).applied():
            import tests.worker.workflow_sandbox.testmodules.invalid_module  # type:ignore[reportUnusedImport]
    assert (
        err.value.qualified_name
        == "tests.worker.workflow_sandbox.testmodules.invalid_module"
    )


def test_workflow_sandbox_importer_repeat_import_skips_import_machinery(
    monkeypatch: pytest.MonkeyPatch,
):
    imported: list[str] = []
    orig_import = importlib.__import__

    def recording_import(
        name: str,
        globals: Any = None,
        locals: Any = None,
        fromlist: Any = (),
        level: int = 0,
    ) -> Any:
        imported.append(name)
        return orig_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(importlib, "__import__", recording_import)
    with Importer(restrictions, RestrictionContext()).applied():
        import tests.worker.workflow_sandbox.testmodules.passthrough_module as passthrough
        import tests.worker.workflow_sandbox.testmodules.stateful_module as stateful

        assert imported
        imported.clear()

        # Loaded modules are served from sys.modules without re-entering importlib
        import typing

        import tests.worker.workflow_sandbox.testmodules as testmodules
        import tests.worker.workflow_sandbox.testmodules.passthrough_module as passthrough_again
        import tests.worker.workflow_sandbox.testmodules.stateful_module as stateful_again

        assert passthrough_again is passthrough
        assert stateful_again is stateful
        assert getattr(testmodules, "stateful_module") is stateful
        assert typing is sys.modules["typing"]
        assert imported == []

        # Imports with a fromlist keep using importlib's full semantics.
        from tests.worker.workflow_sandbox.testmodules import stateful_module

        assert stateful_module is stateful
        assert imported == ["tests.worker.workflow_sandbox.testmodules"]


def test_loaded_module_fast_path_requires_completed_modules(
    monkeypatch: pytest.MonkeyPatch,
):
    top_name = "test_loaded_module_fast_path"
    child_name = f"{top_name}.child"
    top = types.ModuleType(top_name)
    child = types.ModuleType(child_name)
    top_spec = importlib.machinery.ModuleSpec(top_name, loader=None)
    child_spec = importlib.machinery.ModuleSpec(child_name, loader=None)
    top.__spec__ = top_spec
    child.__spec__ = child_spec
    monkeypatch.setitem(sys.modules, top_name, top)
    monkeypatch.setitem(sys.modules, child_name, child)

    assert _loaded_module_for_import(child_name, ("child",), 0) is None
    assert _loaded_module_for_import(child_name, (), 1) is None
    setattr(child_spec, "_initializing", True)
    assert _loaded_module_for_import(child_name, (), 0) is None
    setattr(child_spec, "_initializing", False)
    setattr(top_spec, "_initializing", True)
    assert _loaded_module_for_import(child_name, (), 0) is None
    setattr(top_spec, "_initializing", False)
    assert _loaded_module_for_import(child_name, (), 0) is top


def test_workflow_sandbox_importer_passthrough_module():
    # Import outside of importer
    import tests.worker.workflow_sandbox.testmodules.passthrough_module as outside1
    import tests.worker.workflow_sandbox.testmodules.stateful_module as outside2

    assert outside1.module_state == ["module orig"]
    assert outside2.module_state == ["module orig"]

    # Now import via importer
    with Importer(restrictions, RestrictionContext()).applied():
        import tests.worker.workflow_sandbox.testmodules.passthrough_module as inside1
        import tests.worker.workflow_sandbox.testmodules.stateful_module as inside2

        from .testmodules import stateful_module as inside_relative2

    # Now if we alter inside1, it's passthrough so it affects outside1
    inside1.module_state = ["another val"]
    assert outside1.module_state == ["another val"]
    assert id(inside1) == id(outside1)

    # Confirm relative is same as non-relative
    assert id(inside2) == id(inside_relative2)

    # But if we alter non-passthrough inside2 it does not affect outside2
    inside2.module_state = ["another val"]
    assert outside2.module_state != ["another val"]
    assert id(inside2) != id(outside2)


def test_workflow_sandbox_importer_passthough_context_manager():
    import tests.worker.workflow_sandbox.testmodules.stateful_module as outside

    with Importer(restrictions, RestrictionContext()).applied():
        with workflow.unsafe.imports_passed_through():
            import tests.worker.workflow_sandbox.testmodules.stateful_module as inside
    assert id(outside) == id(inside)


def test_workflow_sandbox_importer_passthrough_all_modules():
    import tests.worker.workflow_sandbox.testmodules.stateful_module as outside

    # Confirm regular restrictions does re-import
    with Importer(restrictions, RestrictionContext()).applied():
        import tests.worker.workflow_sandbox.testmodules.stateful_module as inside1
    assert id(outside) != id(inside1)

    # But that one with all modules passed through does not
    with Importer(
        restrictions.with_passthrough_all_modules(), RestrictionContext()
    ).applied():
        import tests.worker.workflow_sandbox.testmodules.stateful_module as inside2
    assert id(outside) == id(inside2)


def test_workflow_sandbox_importer_invalid_module_members():
    importer = Importer(restrictions, RestrictionContext())
    # Can access the function, no problem
    with importer.applied():
        import tests.worker.workflow_sandbox.testmodules.invalid_module_members

        _ = tests.worker.workflow_sandbox.testmodules.invalid_module_members.invalid_function

    # Cannot call qualified
    with pytest.raises(RestrictedWorkflowAccessError) as err:
        with importer.applied():
            import tests.worker.workflow_sandbox.testmodules.invalid_module_members

            tests.worker.workflow_sandbox.testmodules.invalid_module_members.invalid_function()
    assert (
        err.value.qualified_name
        == "tests.worker.workflow_sandbox.testmodules.invalid_module_members.invalid_function.__call__"
    )

    # Cannot call via from import either
    with pytest.raises(RestrictedWorkflowAccessError) as err:
        with importer.applied():
            from tests.worker.workflow_sandbox.testmodules.invalid_module_members import (
                invalid_function,
            )

            invalid_function()
    assert (
        err.value.qualified_name
        == "tests.worker.workflow_sandbox.testmodules.invalid_module_members.invalid_function.__call__"
    )


def test_workflow_sandbox_importer_sys_module():
    # Import outside to make sure this is in sys.modules
    import tests.worker.workflow_sandbox.testmodules.passthrough_module  # type:ignore[reportUnusedImport]
    import tests.worker.workflow_sandbox.testmodules.stateful_module  # type:ignore[reportUnusedImport]

    with Importer(restrictions, RestrictionContext()).applied():
        # Passthrough should be there but not non-passthrough
        assert sys.modules.get(
            "tests.worker.workflow_sandbox.testmodules.passthrough_module", None
        )
        assert not sys.modules.get(
            "tests.worker.workflow_sandbox.testmodules.stateful_module", None
        )

    disabled_restrictions = dataclasses.replace(
        restrictions, disable_lazy_sys_module_passthrough=True
    )
    with Importer(disabled_restrictions, RestrictionContext()).applied():
        # Neither should be there because lazy sys mod is disabled
        assert not sys.modules.get(
            "tests.worker.workflow_sandbox.testmodules.passthrough_module", None
        )
        assert not sys.modules.get(
            "tests.worker.workflow_sandbox.testmodules.stateful_module", None
        )


def test_thread_local_sys_module_attrs():
    # Python chose not to put everything in MutableMapping they do in dict, see
    # https://bugs.python.org/issue22101. Therefore we manually confirm that
    # every attribute of sys modules is also in thread local sys modules to
    # ensure compatibility.
    for attr in dir(sys.modules):
        getattr(_thread_local_sys_modules, attr)

    # Let's also test "or" and "copy"
    norm = {"foo": 123}
    thread_local = _ThreadLocalSysModules({"foo": 123})  # type: ignore[dict-item]
    assert (norm | {"bar": 456}) == (thread_local | {"bar": 456})  # type: ignore
    norm |= {"baz": 789}
    thread_local |= {"baz": 789}  # type: ignore
    assert norm.copy() == thread_local.copy()


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
