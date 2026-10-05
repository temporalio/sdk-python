import importlib
import sys
import warnings
from types import ModuleType

import pytest
import temporalio.openai_agents as standalone  # pyright: ignore[reportMissingImports]
import temporalio.openai_agents.testing as standalone_testing  # pyright: ignore[reportMissingImports]
import temporalio.openai_agents.workflow as standalone_workflow  # pyright: ignore[reportMissingImports]

import temporalio.contrib
import temporalio.contrib.openai_agents as compatibility
import temporalio.contrib.openai_agents.testing as compatibility_testing
import temporalio.contrib.openai_agents.workflow as compatibility_workflow


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
        actual = getattr(compatibility_module, name)
        expected = getattr(standalone_module, name)
        if compatibility_module is compatibility and isinstance(expected, ModuleType):
            assert isinstance(actual, ModuleType)
        else:
            assert actual is expected


@pytest.mark.parametrize(
    "name,compatibility_module",
    [
        ("testing", compatibility_testing),
        ("workflow", compatibility_workflow),
    ],
)
def test_openai_agents_compatibility_submodules(
    name: str, compatibility_module: ModuleType
) -> None:
    assert (
        importlib.import_module(f"temporalio.contrib.openai_agents.{name}")
        is compatibility_module
    )
    assert compatibility_module.__name__ == f"{compatibility.__name__}.{name}"


def test_openai_agents_compatibility_new_exports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "future_helpers"
    module = ModuleType(f"{standalone.__name__}.{name}")
    exported_value = object()
    alias = f"{compatibility.__name__}.{name}"
    with monkeypatch.context() as patch:
        patch.setattr(
            standalone, "__all__", [*standalone.__all__, name, "future_value"]
        )
        patch.setattr(standalone, name, module, raising=False)
        patch.setattr(standalone, "future_value", exported_value, raising=False)
        patch.delitem(sys.modules, alias, raising=False)
        # Reload adds exports that must be removed when the patched API is restored.
        patch.setattr(compatibility, name, None, raising=False)
        patch.setattr(compatibility, "future_value", None, raising=False)
        importlib.reload(compatibility)
        assert compatibility.__all__ is standalone.__all__
        assert getattr(compatibility, name) is module
        assert getattr(compatibility, "future_value") is exported_value
        assert alias not in sys.modules
    importlib.reload(compatibility)


@pytest.mark.parametrize(
    "legacy,canonical,submodules",
    [
        ("deepagents", "deepagents", ("testing", "workflow")),
        ("google_adk_agents", "google_adk", ("workflow",)),
        ("google_genai", "google_genai", ("testing", "workflow")),
        ("langgraph", "langgraph", ()),
        ("langsmith", "langsmith", ()),
        ("strands", "strands_agents", ("workflow",)),
    ],
)
def test_ai_integration_compatibility_imports(
    monkeypatch: pytest.MonkeyPatch,
    legacy: str,
    canonical: str,
    submodules: tuple[str, ...],
) -> None:
    # Exercise the forwarding contract before the standalone packages are released.
    root = ModuleType(f"temporalio.{canonical}")
    setattr(root, "__path__", [])
    modules = [root]
    for name in submodules:
        module = ModuleType(f"{root.__name__}.{name}")
        setattr(root, name, module)
        modules.append(module)
    for module in modules:
        setattr(module, "future_value", object())
        if not module.__name__.endswith(".workflow"):
            setattr(module, "__all__", ["future_value"])
        monkeypatch.setitem(sys.modules, module.__name__, module)

    compatibility_name = f"temporalio.contrib.{legacy}"
    monkeypatch.setattr(temporalio.contrib, legacy, None, raising=False)
    for module in modules:
        suffix = module.__name__.removeprefix(root.__name__)
        name = f"{compatibility_name}{suffix}"
        monkeypatch.setitem(sys.modules, name, None)
        del sys.modules[name]
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        for module in modules:
            suffix = module.__name__.removeprefix(root.__name__)
            name = f"{compatibility_name}{suffix}"
            forwarded = importlib.import_module(name)
            assert forwarded.__name__ == name
            assert getattr(forwarded, "future_value") is getattr(module, "future_value")
            if hasattr(module, "__all__"):
                assert getattr(forwarded, "__all__") is getattr(module, "__all__")
