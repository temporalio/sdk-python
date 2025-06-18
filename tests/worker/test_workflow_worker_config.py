"""
Run tests in test_workflow.py with different worker configurations.
"""

import inspect

import pytest

import tests.helpers
import tests.worker.test_workflow as test_workflow_module

_worker_configs = [
    pytest.param(
        {
            "max_concurrent_workflow_tasks": 2,
            "max_concurrent_workflow_task_polls": 2,
        },
        id="max_concurrent_workflow_tasks_2_polls_2",
    ),
]


@pytest.fixture(scope="module", autouse=True, params=_worker_configs)
def worker_config(request):
    original_kwargs = tests.helpers.DEFAULT_WORKER_KWARGS.copy()
    tests.helpers.DEFAULT_WORKER_KWARGS.update(request.param)
    yield
    tests.helpers.DEFAULT_WORKER_KWARGS.clear()
    tests.helpers.DEFAULT_WORKER_KWARGS.update(original_kwargs)


for name, fn in inspect.getmembers(test_workflow_module, inspect.isfunction):
    if name.startswith("test_"):
        globals()[name] = fn
