from typing import Callable, Mapping, Optional
import temporalio.client
import concurrent.futures

class Worker:
    def __init__(self,
        client: temporalio.client.Client,
        *,
        task_queue: str,
        activities: Mapping[str, Callable] = {},
        activity_executor: Optional[concurrent.futures.Executor] = None,
        async_activity_executor: Optional[concurrent.futures.Executor] = None,
    ) -> None:
        pass