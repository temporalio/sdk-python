"""Workflow definitions for Functional API E2E tests.

Workflow classes that use compile_functional to run @entrypoint functions.
"""

from __future__ import annotations

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporalio.contrib.langgraph import compile_functional


@workflow.defn
class SimpleFunctionalE2EWorkflow:
    """Simple workflow using functional API.

    Compiles and runs the simple_functional_entrypoint.
    """

    @workflow.run
    async def run(self, input_value: int) -> dict:
        app = compile_functional("e2e_simple_functional")
        return await app.ainvoke(input_value)
