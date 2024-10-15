import asyncio
from typing import List

from temporalio import workflow


@workflow.defn
class GreetingWorkflow:
    def __init__(self) -> None:
        self._pending_greetings: asyncio.Queue[str] = asyncio.Queue()
        self._exit = False

    @workflow.run
    async def run(self) -> List[str]:
        workflow.logger.log_during_replay = True
        workflow.logger.warn("starting")
        # Continually handle from queue or wait for exit to be received
        greetings: List[str] = []
        while True:
            # Wait for queue item or exit
            await workflow.wait_condition(
                lambda: not self._pending_greetings.empty() or self._exit
            )

            # Drain and process queue
            while not self._pending_greetings.empty():
                greetings.append(f"Hello, {self._pending_greetings.get_nowait()}")

            # Exit if complete
            if self._exit:
                return greetings

    @workflow.signal
    async def submit_greeting(self, name: str) -> None:
        print("SIGNALLED")
        await self._pending_greetings.put(name)

    @workflow.signal
    def exit(self) -> None:
        print("SIGNALLED EXIT")
        self._exit = True


