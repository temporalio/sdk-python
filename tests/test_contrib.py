import importlib
import sys
import warnings
from types import ModuleType

import pytest


@pytest.mark.parametrize(
    "legacy,canonical,submodules",
    [
        pytest.param(
            "deepagents",
            "deepagents",
            ("testing", "workflow"),
            marks=pytest.mark.skipif(
                sys.version_info < (3, 11), reason="Deep Agents requires Python 3.11"
            ),
        ),
        ("google_adk_agents", "google_adk", ("workflow",)),
        ("google_genai", "google_genai", ("testing", "workflow")),
        ("langgraph", "langgraph", ()),
        ("langsmith", "langsmith", ()),
        ("openai_agents", "openai_agents", ("testing", "workflow")),
        ("strands", "strands_agents", ("workflow",)),
    ],
)
def test_ai_integration_compatibility_imports(
    legacy: str,
    canonical: str,
    submodules: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for suffix in ("", *(f".{name}" for name in submodules)):
        # Load lazy exports too so dependency warnings do not fail the shim check.
        module = importlib.import_module(f"temporalio.{canonical}{suffix}")
        exported = {
            name: getattr(module, name)
            for name in getattr(
                module,
                "__all__",
                [name for name in vars(module) if not name.startswith("_")],
            )
        }
        name = f"temporalio.contrib.{legacy}{suffix}"
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            forwarded = importlib.import_module(name)
            importlib.reload(forwarded)
            assert importlib.import_module(name) is forwarded
        assert forwarded.__name__ == name
        assert set(exported) == {
            name for name in vars(forwarded) if not name.startswith("_")
        }
        for exported_name, value in exported.items():
            assert getattr(forwarded, exported_name) is value
        if hasattr(module, "__all__"):
            assert getattr(forwarded, "__all__") is getattr(module, "__all__")

    standalone = importlib.import_module(f"temporalio.{canonical}")
    compatibility = importlib.import_module(f"temporalio.contrib.{legacy}")
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
