from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import Any, Awaitable, Mapping, Optional, Union

import temporalio
import temporalio.client
import temporalio.converter


@dataclass
class TLSConfig:
    server_root_ca_cert: Optional[Union[str, bytes]] = None
    domain: Optional[str] = None
    client_cert: Optional[Union[str, bytes]] = None
    client_private_key: Optional[Union[str, bytes]] = None


class WorkflowIDReusePolicy(Enum):
    ALLOW_DUPLICATE = 1
    ALLOW_DUPLICATE_FAILED_ONLY = 2
    REJECT_DUPLICATE = 3


class Client:
    @staticmethod
    async def connect(
        addr: str,
        *,
        namespace: str = "default",
        data_converter: temporalio.converter.DataConverter = temporalio.converter.default(),
        headers: Mapping[str, str] = {},
        identity: Optional[str] = None,
        tls_config: Optional[TLSConfig] = None,
        retry_policy: Optional[temporalio.RetryPolicy] = None
    ) -> "Client":
        client = Client(
            addr,
            namespace=namespace,
            data_converter=data_converter,
            headers=headers,
            identity=identity,
            tls_config=tls_config,
            retry_policy=retry_policy,
        )
        await client.ready()
        return client

    service: Awaitable[temporalio.client.WorkflowService]

    def __init__(
        self,
        addr: str,
        *,
        namespace: str = "default",
        data_converter: temporalio.converter.DataConverter = temporalio.converter.default(),
        headers: Mapping[str, str] = {},
        identity: Optional[str] = None,
        tls_config: Optional[TLSConfig] = None,
        retry_policy: Optional[temporalio.RetryPolicy] = None
    ) -> None:
        raise NotImplementedError

    async def __aenter__(self) -> "Client":
        await self.ready()
        return self

    async def __aexit__(self) -> None:
        await self.close()

    async def ready(self) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    async def start_workflow(
        self,
        workflow: str,
        *args: Any,
        task_queue: str,
        id: Optional[str] = None,
        execution_timeout: Optional[timedelta] = None,
        run_timeout: Optional[timedelta] = None,
        task_timeout: Optional[timedelta] = None,
        id_reuse_policy: WorkflowIDReusePolicy = WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        retry_policy: Optional[temporalio.RetryPolicy] = None,
        cron_schedule: Optional[str] = None,
        memo: Mapping[str, temporalio.Convertible] = {},
        search_attributes: Mapping[str, temporalio.Convertible] = {},
        header: Mapping[str, temporalio.Convertible] = {}
    ) -> temporalio.client.WorkflowHandle[Optional[temporalio.Convertible]]:
        raise NotImplementedError

    async def execute_workflow(
        self,
        workflow: str,
        *args: Any,
        task_queue: str,
        id: Optional[str] = None,
        execution_timeout: Optional[timedelta] = None,
        run_timeout: Optional[timedelta] = None,
        task_timeout: Optional[timedelta] = None,
        id_reuse_policy: WorkflowIDReusePolicy = WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        retry_policy: Optional[temporalio.RetryPolicy] = None,
        cron_schedule: Optional[str] = None,
        memo: Mapping[str, temporalio.Convertible] = {},
        search_attributes: Mapping[str, temporalio.Convertible] = {},
        header: Mapping[str, temporalio.Convertible] = {}
    ) -> Optional[temporalio.Convertible]:
        handle = await self.start_workflow(
            workflow,
            *args,
            task_queue=task_queue,
            id=id,
            execution_timeout=execution_timeout,
            run_timeout=run_timeout,
            task_timeout=task_timeout,
            id_reuse_policy=id_reuse_policy,
            retry_policy=retry_policy,
            cron_schedule=cron_schedule,
            memo=memo,
            search_attributes=search_attributes,
            header=header
        )
        return await handle.result()

    def get_workflow_handle(
        self, id: str, run_id: Optional[str] = None
    ) -> temporalio.client.WorkflowHandle[Optional[temporalio.Convertible]]:
        return temporalio.client.WorkflowHandle(self, id, run_id)
