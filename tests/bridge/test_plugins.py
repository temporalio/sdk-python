from collections import Counter

import pytest

import temporalio.bridge.temporal_sdk_bridge
from temporalio.client import Client
from temporalio.plugin import SimplePlugin
from temporalio.worker import Worker
from tests.worker.test_worker import never_run_activity


async def test_worker_plugin_names_forwarded_to_core(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_plugins: list[str] = []

    original_new_worker = temporalio.bridge.temporal_sdk_bridge.new_worker

    def new_worker_wrapper(runtime_ref, client_ref, config):
        nonlocal captured_plugins
        captured_plugins = list(config.plugins)
        return original_new_worker(runtime_ref, client_ref, config)

    monkeypatch.setattr(
        temporalio.bridge.temporal_sdk_bridge,
        "new_worker",
        new_worker_wrapper,
    )

    plugin1 = SimplePlugin("test-worker-plugin1")
    plugin2 = SimplePlugin("test-worker-plugin2")
    Worker(
        client,
        task_queue="queue",
        activities=[never_run_activity],
        plugins=[plugin1, plugin2],
    )
    # Use counter to compare unordered lists
    assert Counter(captured_plugins) == Counter([plugin1.name(), plugin2.name()])
