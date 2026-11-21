import steps as scoring  # type: ignore[import-not-found]
from steps import remember, remembered  # type: ignore[import-not-found]
from steps import score as calculate_score  # type: ignore[import-not-found]

import temporalio.workflow as workflow


def expected_labels(value: int) -> list[str]:
    return [f"v{value}", f"{value}/y/z!?"]


@workflow.defn
class AddressableWorkflow:
    def __init__(self) -> None:
        self.inputs: list[int] = []

    @workflow.signal
    def advance(self, value: int) -> None:
        self.inputs.append(value)

    @workflow.query
    def preview(self) -> list[str]:
        return remembered()

    @workflow.run
    async def run(self) -> list[tuple[int, list[str]]]:
        results: list[tuple[int, list[str]]] = []
        while len(results) < 3:
            await workflow.wait_condition(lambda: len(self.inputs) > len(results))
            value = self.inputs[len(results)]
            remember(str(value), "y", "z", suffix="!", mark="?")
            labels = remembered()
            # Check the revision's default and decorator during replay too.
            assert labels == expected_labels(value)
            assert labels is remembered()
            score, echoed_labels = await calculate_score(value, labels=labels)
            assert echoed_labels is labels
            results.append((score, echoed_labels))
            workflow.upsert_memo({"completed_calls": len(results)})
        return results


@workflow.defn
class AddressableBatch:
    @workflow.run
    async def run(self) -> int:
        total = 0
        for _ in range(1000):
            value, _ = await scoring.score(-1)
            total += value
        return total
