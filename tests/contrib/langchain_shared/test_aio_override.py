"""The shared LangSmith ``aio_to_thread`` override installer."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from temporalio.contrib._langchain import _aio_to_thread as mod

pytest.importorskip("langsmith")


@pytest.fixture(autouse=True)
def reset_installed_flag(monkeypatch: pytest.MonkeyPatch):
    """Isolate the module-level install-once flag per test."""
    monkeypatch.setattr(mod, "_installed", False)
    yield


def test_installs_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import langsmith

    calls: list[Any] = []
    monkeypatch.setattr(
        langsmith, "set_runtime_overrides", lambda **kw: calls.append(kw)
    )
    mod.install_aio_to_thread_override()
    mod.install_aio_to_thread_override()
    mod.install_aio_to_thread_override()
    assert len(calls) == 1
    assert calls[0] == {"aio_to_thread": mod._temporal_aio_to_thread}


def test_failure_leaves_flag_unset_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import langsmith

    boom = RuntimeError("boom")

    def _raise(**_kw: Any) -> None:
        raise boom

    monkeypatch.setattr(langsmith, "set_runtime_overrides", _raise)
    with pytest.raises(RuntimeError):
        mod.install_aio_to_thread_override()
    assert mod._installed is False

    # A later call retries and succeeds.
    calls: list[Any] = []
    monkeypatch.setattr(
        langsmith, "set_runtime_overrides", lambda **kw: calls.append(kw)
    )
    mod.install_aio_to_thread_override()
    assert len(calls) == 1
    assert mod._installed is True


def test_override_delegates_outside_workflows() -> None:
    """Outside a workflow the default thread hop is used unchanged."""

    async def default(_ctx: Any, func: Any, *args: Any, **kwargs: Any) -> Any:
        return ("default", func(*args, **kwargs))

    result = asyncio.run(
        mod._temporal_aio_to_thread(default, object(), lambda x: x + 1, 41)
    )
    assert result == ("default", 42)
