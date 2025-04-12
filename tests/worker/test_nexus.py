from dataclasses import dataclass
from enum import IntEnum
from typing import Optional, Union

import nexus
import nexus.handler

import temporalio.nexus
import temporalio.nexus.handler
from temporalio import workflow


# -----------------------------------------------------------------------------
# Service interface
#
class ResponseType(IntEnum):
    SYNC = 1
    ASYNC = 2


@dataclass
class MyInput:
    response_type: ResponseType
    workflow_id: Optional[str]


@dataclass
class MyOutput:
    val: str


@nexus.service
class MyService:
    my_operation: nexus.Operation[MyInput, MyOutput]


# -----------------------------------------------------------------------------
# Service implementation
#


@workflow.defn
class MyHandlerWorkflow:
    @workflow.run
    async def run(self) -> MyOutput:
        return MyOutput("workflow result")


class MyOperation:
    async def start(
        self, input: MyInput, options: nexus.handler.StartOperationOptions
    ) -> Union[
        MyOutput,
        temporalio.nexus.handler.AsyncWorkflowOperationResult[MyOutput],
    ]:
        if input.response_type == ResponseType.SYNC:
            return MyOutput(val="sync response")
        elif input.response_type == ResponseType.ASYNC:
            assert input.workflow_id
            return await temporalio.nexus.handler.start_workflow(
                MyHandlerWorkflow.run,
                id=input.workflow_id,
                options=options,
            )
        else:
            raise ValueError

    async def cancel(
        self, token: str, options: nexus.handler.CancelOperationOptions
    ) -> None:
        return await temporalio.nexus.handler.cancel_workflow(token, options)

    async def fetch_info(self, *args, **kwargs):
        raise NotImplementedError

    async def fetch_result(self, *args, **kwargs):
        raise NotImplementedError


@nexus.handler.service
class MyServiceImpl:
    @nexus.handler.operation
    def my_operation(self) -> nexus.handler.Operation[MyInput, MyOutput]:
        return MyOperation()
