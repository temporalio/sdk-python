from scoring import score  # type: ignore[import-not-found]

import temporalio.workflow as workflow


@workflow.defn
class AddressableNestedWorkflow:
    def __init__(self) -> None:
        self._ready = False

    @workflow.signal
    def advance(self) -> None:
        self._ready = True

    @workflow.run
    async def run(self) -> list[int]:
        first, initial_score = await score(1)
        await workflow.wait_condition(lambda: self._ready)
        second, current_score = await score(2)
        assert initial_score is current_score
        return [first, second]
