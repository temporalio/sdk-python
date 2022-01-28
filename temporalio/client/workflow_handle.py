from typing import Generic, Optional, TypeVar

import temporalio
import temporalio.client

T = TypeVar("T")


class WorkflowHandle(Generic[T]):
    _client: temporalio.client.Client
    id: str
    run_id: Optional[str] = None

    def __init__(
        self, client: temporalio.client.Client, id: str, run_id: Optional[str] = None
    ) -> None:
        self._client = client
        self.id = id
        self.run_id = run_id

    async def result(self) -> T:
        raise NotImplementedError

    async def cancel(self) -> None:
        raise NotImplementedError

    async def describe(self) -> temporalio.client.WorkflowExecution:
        raise NotImplementedError

    async def query(
        self, name: str, *args: temporalio.Convertible
    ) -> temporalio.Convertible:
        raise NotImplementedError

    async def signal(self, name: str, *args: temporalio.Convertible) -> None:
        raise NotImplementedError

    async def terminate(self, *, reason: Optional[str] = None) -> None:
        raise NotImplementedError
