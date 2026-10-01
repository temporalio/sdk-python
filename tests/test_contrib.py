import importlib
import sys
import warnings
from types import ModuleType

import pytest
import temporalio.contrib.openai_agents.testing as compatibility_testing  # pyright: ignore[reportMissingImports]
import temporalio.contrib.openai_agents.workflow as compatibility_workflow  # pyright: ignore[reportMissingImports]
import temporalio.openai_agents as standalone  # pyright: ignore[reportMissingImports]
import temporalio.openai_agents.testing as standalone_testing  # pyright: ignore[reportMissingImports]
import temporalio.openai_agents.workflow as standalone_workflow  # pyright: ignore[reportMissingImports]

import temporalio.contrib.openai_agents as compatibility


def test_openai_agents_compatibility_imports_without_warnings() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        importlib.reload(compatibility)
        importlib.reload(compatibility_testing)
        importlib.reload(compatibility_workflow)


@pytest.mark.parametrize(
    "compatibility_module,standalone_module",
    [
        (compatibility, standalone),
        (compatibility_testing, standalone_testing),
        (compatibility_workflow, standalone_workflow),
    ],
)
def test_openai_agents_compatibility_imports(
    compatibility_module: ModuleType, standalone_module: ModuleType
) -> None:
    def exported_names(module: ModuleType) -> set[str]:
        return set(
            getattr(
                module,
                "__all__",
                [name for name in vars(module) if not name.startswith("_")],
            )
        )

    assert exported_names(compatibility_module) == exported_names(standalone_module)
    for name in exported_names(standalone_module):
        assert getattr(compatibility_module, name) is getattr(standalone_module, name)


@pytest.mark.parametrize(
    "name",
    [
        name
        for name in standalone.__all__
        if isinstance(getattr(standalone, name), ModuleType)
    ],
)
def test_openai_agents_compatibility_submodules(name: str) -> None:
    standalone_module = importlib.import_module(f"temporalio.openai_agents.{name}")
    assert (
        importlib.import_module(f"temporalio.contrib.openai_agents.{name}")
        is standalone_module
    )
    assert getattr(compatibility, name) is standalone_module


def test_openai_agents_compatibility_new_submodule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "future_helpers"
    module = ModuleType(f"{standalone.__name__}.{name}")
    alias = f"{compatibility.__name__}.{name}"
    with monkeypatch.context() as patch:
        patch.setattr(standalone, "__all__", [*standalone.__all__, name])
        patch.setattr(standalone, name, module, raising=False)
        # Track the new alias and export so reload does not leak into other tests.
        patch.setitem(sys.modules, alias, None)
        patch.setattr(compatibility, name, None, raising=False)
        importlib.reload(compatibility)
        assert importlib.import_module(alias) is module
        assert getattr(compatibility, name) is module
    importlib.reload(compatibility)
