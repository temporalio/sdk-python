from steps import score  # type: ignore[import-not-found]

import temporalio.workflow as workflow


@workflow.defn
class AddressableWorkflow:
    def __init__(self) -> None:
        self.inputs: list[int] = []

    @workflow.signal
    def advance(self, value: int) -> None:
        self.inputs.append(value)

    @workflow.run
    async def run(self) -> list[int]:
        results: list[int] = []
        while len(results) < 3:
            await workflow.wait_condition(lambda: len(self.inputs) > len(results))
            results.append(await score(self.inputs[len(results)]))
            workflow.upsert_memo({"completed_calls": len(results)})
        return results


@workflow.defn
class AddressableBatch:
    @workflow.run
    async def run(self) -> int:
        total = 0
        for _ in range(1000):
            total += await score(-1)
        return total
