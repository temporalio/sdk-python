"""Import a packaged extension during an activation using the default converter."""

import importlib

import temporalio.workflow as workflow


@workflow.defn
class WasmNativeExtension:
    @workflow.run
    async def run(self, check_clock: bool) -> int:
        extension = importlib.import_module("example_native.probe")
        return int(extension.wall_clock() if check_clock else extension.answer())
