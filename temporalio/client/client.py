
from datetime import timedelta
from enum import Enum
import os
import socket
from typing import Any, Awaitable, Generic, Optional, TypeVar, Union, overload
import uuid
from temporalio.api.enums.v1.workflow_pb2 import WorkflowIdReusePolicy
import temporalio.client
import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.api.taskqueue.v1
import temporalio.api.workflowservice.v1
import temporalio.converter
import temporalio.util.proto
import temporalio

class WorkflowIDReusePolicy(Enum):
    ALLOW_DUPLICATE = temporalio.api.enums.v1.WorkflowIdReusePolicy.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE
    ALLOW_DUPLICATE_FAILED_ONLY = temporalio.api.enums.v1.WorkflowIdReusePolicy.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE_FAILED_ONLY
    REJECT_DUPLICATE = temporalio.api.enums.v1.WorkflowIdReusePolicy.WORKFLOW_ID_REUSE_POLICY_REJECT_DUPLICATE

class Client:

    @staticmethod
    async def connect(target_url: str, *, namespace: str="default", data_converter: temporalio.converter.DataConverter = temporalio.converter.default()) -> "Client":
        return Client(await temporalio.client.WorkflowService.connect(target_url), namespace=namespace, data_converter=data_converter)

    def __init__(self, service: temporalio.client.WorkflowService, *, namespace: str="default", data_converter: temporalio.converter.DataConverter = temporalio.converter.default()):
        self._service = service
        self._namespace = namespace
        self._data_converter = data_converter

    @property
    def service(self) -> temporalio.client.WorkflowService:
        return self._service

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def data_converter(self) -> temporalio.converter.DataConverter:
        return self._data_converter

    async def start_workflow(
        self,
        workflow: str,
        *args: Any,
        task_queue: str,
        # TODO(cretz): Should we require this?
        id: str = str(uuid.uuid4()),
        execution_timeout: Optional[timedelta] = None,
        run_timeout: Optional[timedelta] = None,
        task_timeout: Optional[timedelta] = None,
        identity: str = f"{os.getpid()}@{socket.gethostname()}",
        id_reuse_policy: WorkflowIdReusePolicy = WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        retry_policy: Optional[temporalio.RetryPolicy] = None,
        cron_schedule: Optional[str] = None
        # TODO(cretz): Signal with start
        # start_signal: Optional[str] = None,
        # start_signal_args: list[Any] = [],
        # ... additional options omitted for brevity
    ) -> temporalio.WorkflowHandle[Any]:
        input = None
        if len(args) > 0:
            input = temporalio.api.common.v1.Payloads(payloads = await self._data_converter.encode(list(args)))
        proto_retry_policy = None
        if retry_policy is not None:
            proto_retry_policy = retry_policy.to_proto()
        req = temporalio.api.workflowservice.v1.StartWorkflowExecutionRequest(
            namespace=self._namespace,
            workflow_id=id,
            workflow_type=temporalio.api.common.v1.WorkflowType(name=workflow),
            task_queue=temporalio.api.taskqueue.v1.TaskQueue(name=task_queue),
            input=input,
            workflow_execution_timeout=temporalio.util.proto.from_timedelta(execution_timeout),
            workflow_run_timeout=temporalio.util.proto.from_timedelta(run_timeout),
            workflow_task_timeout=temporalio.util.proto.from_timedelta(task_timeout),
            identity=identity,
            request_id=str(uuid.uuid4()),
            workflow_id_reuse_policy = id_reuse_policy,
            retry_policy = None if retry_policy is None else retry_policy.to_proto(),
            cron_schedule: typing.Text = ...,
            memo: typing.Optional[temporal.api.common.v1.message_pb2.Memo] = ...,
            search_attributes: typing.Optional[temporal.api.common.v1.message_pb2.SearchAttributes] = ...,
            header: typing.Optional[temporal.api.common.v1.message_pb2.Header] = ...,
        )
        # TODO(cretz): The rest
        pass

T = TypeVar("T")

class WorkflowHandle(Generic[T]):
    def __init__(
        self,
        client: Client,
        id: str,
        *,
        run_id: Optional[str] = None
    ) -> None:
        self._client = client
        self._id = id
        self._run_id = run_id

    async def result(self) -> T:
        pass

    async def cancel(self):
        pass

    async def describe(self) -> temporalio.WorkflowExecution:
        pass

    async def query(self, name: str, *args: Any) -> Any:
        pass

    async def signal(self, name: str, *args: Any):
        pass

    async def terminate(self, *, reason: Optional[str] = None):
        pass