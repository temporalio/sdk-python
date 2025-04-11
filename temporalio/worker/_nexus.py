"""Nexus worker"""

from __future__ import annotations

import asyncio
import logging
import pprint
from typing import (
    Any,
    Callable,
    Sequence,
    Union,
)

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

            print(f"🟢 _NexusWorker received poll response: {task}")

            # TODO: Correct way to examine and classify task proto
            if task.HasField("task"):
                if task.task.request.HasField("start_operation"):
                    await self._handle_start_operation(
                        task.task.request.start_operation, task.task.task_token
                    )
                elif task.task.request.HasField("cancel_operation"):
                    await self._handle_cancel_operation(
                        task.task.request.cancel_operation,
                        task.task.request.cancel_operation.operation_token,
                    )
                else:
                    raise NotImplementedError(
                        f"Invalid Nexus task request: {task.task.request}"
                    )
            else:
                # TODO(dan): handle cancel_task
                raise NotImplementedError(f"Invalid Nexus task: {task}")

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

        result = await operation.start(input, options)
        if isinstance(result, nexusrpc.handler.AsyncOperationResult):
            print(
                f"🟢 Nexus operation {request.operation} started with async response {result}"
            )
            # See `rg 'complete_nexus_task(NexusTaskCompletion'` in sdk-core
            op_resp = temporalio.api.nexus.v1.StartOperationResponse(
                async_success=temporalio.api.nexus.v1.StartOperationResponse.Async(
                    # TODO(dan): operation_token
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
            completed=temporalio.api.nexus.v1.Response(start_operation=op_resp),
        )
        await self._bridge_worker().complete_nexus_task(completion)

    async def _handle_cancel_operation(
        self,
        request: temporalio.api.nexus.v1.CancelOperationRequest,
        operation_token: str,
    ) -> None:
        operation = self._get_operation(request)
        # TODO(dan): header
        await operation.cancel(
            token=operation_token,
            options=nexusrpc.handler.CancelOperationOptions(),
        )

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
