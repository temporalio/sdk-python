"""Nexus worker"""

from __future__ import annotations

import asyncio
import json
import logging
import pprint
from typing import (
    Any,
    Callable,
    Sequence,
    Union,
)

import google.protobuf.json_format
import nexusrpc.handler

import temporalio.activity
import temporalio.api.common.v1
import temporalio.api.nexus.v1
import temporalio.bridge.client
import temporalio.bridge.proto
import temporalio.bridge.proto.activity_result
import temporalio.bridge.proto.activity_task
import temporalio.bridge.proto.common
import temporalio.bridge.proto.nexus
import temporalio.bridge.runtime
import temporalio.bridge.worker
import temporalio.client
import temporalio.common
import temporalio.converter
import temporalio.exceptions
import temporalio.nexus
import temporalio.nexus.handler

from ._interceptor import Interceptor

logger = logging.getLogger(__name__)


class _NexusWorker:
    def __init__(
        self,
        *,
        bridge_worker: Callable[[], temporalio.bridge.worker.Worker],
        client: temporalio.client.Client,
        task_queue: str,
        nexus_services: Sequence[Any],
        data_converter: temporalio.converter.DataConverter,
        interceptors: Sequence[Interceptor],
        metric_meter: temporalio.common.MetricMeter,
    ) -> None:
        # TODO(dan): make it possible to query task queue of bridge worker
        # instead of passing unused task_queue into _NexusWorker,
        # _ActivityWorker, etc?
        self._bridge_worker = bridge_worker
        self._client = client
        self._task_queue = task_queue

        self._nexus_services = self._validate_nexus_services(nexus_services)
        print(
            f"🟠 Nexus worker registered Nexus services: {pprint.pformat(self._nexus_services)}"
        )
        self._data_converter = data_converter
        # TODO(dan): interceptors
        self._interceptors = interceptors
        # TODO(dan): metric_meter
        self._metric_meter = metric_meter

    def _validate_nexus_services(
        self, nexus_services: Sequence[Any]
    ) -> dict[str, dict[str, nexusrpc.handler.Operation]]:
        # TODO(dan): Fail if multiple services implement the same service interface.
        nexus_services_by_name: dict[str, dict[str, nexusrpc.handler.Operation]] = {}
        for service in nexus_services:
            defn = nexusrpc.handler._NexusServiceDefinition.from_implementation(service)
            nexus_services_by_name[defn.name] = {
                op_name: op_factory(service)
                for op_name, op_factory in defn.operation_factories.items()
            }
        return nexus_services_by_name

    async def run(self) -> None:
        while True:
            print("_NexusWorker running")

            try:
                poll_task = asyncio.create_task(self._bridge_worker().poll_nexus_task())
            except Exception as err:
                raise RuntimeError("Nexus worker failed") from err

            print("🟠 _NexusWorker sent poll request")

            task = await poll_task

            # message NexusTask {
            #     oneof variant {
            #         // A nexus task from server
            #         temporal.api.workflowservice.v1.PollNexusTaskQueueResponse task = 1;
            #         // A request by Core to notify an in-progress operation handler that it should cancel. This
            #         // is distinct from a `CancelOperationRequest` from the server, which results from the user
            #         // requesting the cancellation of an operation. Handling this variant should result in
            #         // something like cancelling a cancellation token given to the user's operation handler.
            #         //
            #         // These do not count as a separate task for the purposes of completing all issued tasks,
            #         // but rather count as a sort of modification to the already-issued task which is being
            #         // cancelled.
            #         //
            #         // EX: Core knows the nexus operation has timed out, and it does not make sense for the
            #         // user's operation handler to continue doing work.
            #         CancelNexusTask cancel_task = 2;
            #     }
            # }

            task_python = json.loads(google.protobuf.json_format.MessageToJson(task))
            op = (
                "cancel_task"
                if "cancelTask" in task_python
                else "start"
                if "startOperation" in task_python.get("task", {}).get("request", {})
                else "cancel"
                if "cancelOperation" in task_python.get("task", {}).get("request", {})
                else None
            )
            assert op, f"Failed to classify Nexus task: {task_python}"
            print(f"🟢 _NexusWorker received '{op}' operation")

            # TODO: Correct way to examine and classify task proto
            if task.HasField("task"):
                if task.task.request.HasField("start_operation"):
                    await self._handle_start_operation(
                        task.task.request.start_operation, task.task.task_token
                    )
                elif task.task.request.HasField("cancel_operation"):
                    await self._handle_cancel_operation(
                        task.task.request.cancel_operation, task.task.task_token
                    )
                else:
                    raise NotImplementedError(
                        f"Invalid Nexus task request: {task.task.request}"
                    )
            elif task.HasField("cancel_task"):
                print(
                    f"🟠 _NexusWorker received cancel_task, reason: {task.cancel_task.reason}. "
                    "TODO: handling not implemented; ignoring."
                )
                # TODO(dan): handle cancel_task
            else:
                raise NotImplementedError(f"Invalid Nexus task: {task}")

    # Only call this if run() raised an error
    async def drain_poll_queue(self) -> None:
        while True:
            try:
                # Take all tasks and say we can't handle them
                task = await self._bridge_worker().poll_nexus_task()
                completion = temporalio.bridge.proto.nexus.NexusTaskCompletion(
                    task_token=task.task.task_token
                )
                completion.error.failure.message = "Worker shutting down"
                await self._bridge_worker().complete_nexus_task(completion)
            except temporalio.bridge.worker.PollShutdownError:
                return

    # TODO(dan): is it correct to import from temporalio.api.nexus?
    # Why are these things not exposed in temporalio.bridge?
    async def _handle_start_operation(
        self, request: temporalio.api.nexus.v1.StartOperationRequest, task_token: bytes
    ) -> None:
        operation = self._get_operation(request)

        print(
            f"🟠 Starting operation {request.operation} with payload {request.payload}"
        )

        # TODO(dan): HACK. See activity_def.arg_types in _activity.py
        arg_types, _ = temporalio.common._type_hints_from_func(operation.start)

        [input] = await self._data_converter.decode(
            [request.payload],
            type_hints=[arg_types[0]] if arg_types else None,
        )

        # TODO(dan): header
        options = nexusrpc.handler.StartOperationOptions(
            callback_url=request.callback,
            links=[
                nexusrpc.handler.Link(url=l.url, type=l.type) for l in request.links
            ],
            callback_header=dict(request.callback_header),
        )
        temporalio.nexus.handler._current_context.set(
            temporalio.nexus.handler._Context(
                client=self._client,
                task_queue=self._task_queue,
            )
        )

        # message NexusTaskCompletion {
        #     bytes task_token = 1;
        #     oneof status {
        #         temporal.api.nexus.v1.Response completed = 2;
        #         temporal.api.nexus.v1.HandlerError error = 3;
        #         bool ack_cancel = 4;
        #     }
        # }

        result = await operation.start(input, options)
        if isinstance(result, nexusrpc.handler.AsyncOperationResult):
            print(
                f"🟢 Nexus operation {request.operation} started with async response {result}"
            )
            # TODO(dan): is it not valid to instantiate proto objects with an argument?
            # "When called, it accepts no arguments and returns a new featureless instance
            # that has no instance attributes and cannot be given any.""

            # TODO(dan): python proto instantiation
            op_resp = temporalio.api.nexus.v1.StartOperationResponse(
                async_success=temporalio.api.nexus.v1.StartOperationResponse.Async(
                    # TODO(dan): change core protos to use operation_token instead of operation_id
                    operation_id=result.token,
                    # TODO(dan): links
                    links=[],
                )
            )

        else:
            [payload] = await self._data_converter.encode([result])
            op_resp = temporalio.api.nexus.v1.StartOperationResponse(
                sync_success=temporalio.api.nexus.v1.StartOperationResponse.Sync(
                    payload=payload
                )
            )
        completion = temporalio.bridge.proto.nexus.NexusTaskCompletion(
            task_token=task_token,
            # TODO(dan): python proto instantiation
            completed=temporalio.api.nexus.v1.Response(start_operation=op_resp),
        )
        await self._bridge_worker().complete_nexus_task(completion)

    async def _handle_cancel_operation(
        self, request: temporalio.api.nexus.v1.CancelOperationRequest, task_token: bytes
    ) -> None:
        operation = self._get_operation(request)
        # TODO(dan): header
        await operation.cancel(
            token=request.operation_token,
            options=nexusrpc.handler.CancelOperationOptions(),
        )
        # message NexusTaskCompletion {
        #     bytes task_token = 1;
        #     oneof status {
        #         temporal.api.nexus.v1.Response completed = 2;
        #         temporal.api.nexus.v1.HandlerError error = 3;
        #         bool ack_cancel = 4;
        #     }
        # }
        completion = temporalio.bridge.proto.nexus.NexusTaskCompletion(
            task_token=task_token,
            # TODO(dan): python proto instantiation
            completed=temporalio.api.nexus.v1.Response(
                cancel_operation=temporalio.api.nexus.v1.CancelOperationResponse()
            ),
        )
        await self._bridge_worker().complete_nexus_task(completion)

    def _get_operation(
        self,
        request: Union[
            temporalio.api.nexus.v1.StartOperationRequest,
            temporalio.api.nexus.v1.CancelOperationRequest,
        ],
    ) -> nexusrpc.handler.Operation:
        service = self._nexus_services.get(request.service)
        if service is None:
            raise RuntimeError(
                f"Nexus service '{request.service}' has not been registered with this worker."
            )
        operation = service.get(request.operation)
        if operation is None:
            raise RuntimeError(
                f"Nexus service '{request.service}' has no operation '{request.operation}'."
            )
        return operation
