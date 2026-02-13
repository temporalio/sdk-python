"""Temporal Nexus handler.

Replaces nexusrpc.handler.Handler with a Temporal-specific implementation that
uses Temporal interceptors instead of nexusrpc middleware, and deserializes
Nexus operation input using the Temporal data converter directly (without the
nexusrpc Serializer/LazyValue/Content abstractions).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Awaitable, Mapping, Sequence
from functools import reduce
from typing import Any, cast

import nexusrpc
from nexusrpc.handler import (
    CancelOperationContext,
    OperationHandler,
    StartOperationContext,
    StartOperationResultAsync,
    StartOperationResultSync,
)
from nexusrpc.handler._core import ServiceHandler

import temporalio.api.common.v1
import temporalio.converter

from temporalio.worker import (
    ExecuteNexusOperationCancelInput,
    ExecuteNexusOperationStartInput,
    Interceptor,
    NexusOperationInboundInterceptor,
)
from temporalio.nexus import is_async_callable

OperationHandlerResult = StartOperationResultSync[Any] | StartOperationResultAsync


class _NexusOperationInboundInterceptorImpl(NexusOperationInboundInterceptor):
    """Terminal interceptor that delegates to the actual OperationHandler."""

    def __init__(  # pyright: ignore[reportMissingSuperCall]
        self,
        handler: OperationHandler[Any, Any],
        executor: concurrent.futures.Executor | None,
    ) -> None:
        self._handler = handler
        self._executor = executor

    async def execute_nexus_operation_start(
        self, input: ExecuteNexusOperationStartInput
    ) -> StartOperationResultSync[Any] | StartOperationResultAsync:
        if is_async_callable(self._handler.start):
            return await self._handler.start(input.ctx, input.input)
        else:
            assert self._executor
            return await cast(
                Awaitable[OperationHandlerResult],
                asyncio.get_event_loop().run_in_executor(
                    self._executor, self._handler.start, input.ctx, input.input
                ),
            )

    async def execute_nexus_operation_cancel(
        self, input: ExecuteNexusOperationCancelInput
    ) -> None:
        if is_async_callable(self._handler.cancel):
            await self._handler.cancel(input.ctx, input.token)
        else:
            assert self._executor
            self._executor.submit(self._handler.cancel, input.ctx, input.token).result()


class _TemporalNexusHandler:  # type:ignore[reportUnusedClass]
    """Temporal-specific Nexus handler.

    Replaces nexusrpc.handler.Handler. Uses Temporal interceptors instead of
    nexusrpc middleware, and deserializes input using the Temporal data
    converter directly.
    """

    def __init__(
        self,
        user_service_handlers: Sequence[Any],
        interceptors: Sequence[Interceptor],
        data_converter: temporalio.converter.DataConverter,
        executor: concurrent.futures.Executor | None,
    ) -> None:
        self._interceptors = interceptors
        self._data_converter = data_converter
        self._executor = executor
        self._service_handlers = self._register_service_handlers(user_service_handlers)
        if not self._executor:
            self._validate_all_operation_handlers_are_async()

    def _register_service_handlers(
        self, user_service_handlers: Sequence[Any]
    ) -> Mapping[str, ServiceHandler]:
        service_handlers: dict[str, ServiceHandler] = {}
        for sh in user_service_handlers:
            if isinstance(sh, type):
                raise TypeError(
                    f"Expected a service instance, but got a class: {type(sh)}. "
                    "Nexus service handlers must be supplied as instances, not classes."
                )
            if not isinstance(sh, ServiceHandler):
                sh = ServiceHandler.from_user_instance(sh)
            if sh.service.name in service_handlers:
                raise RuntimeError(
                    f"Service '{sh.service.name}' has already been registered."
                )
            service_handlers[sh.service.name] = sh
        return service_handlers

    def _get_service_handler(self, service_name: str) -> ServiceHandler:
        service = self._service_handlers.get(service_name)
        if service is None:
            raise nexusrpc.HandlerError(
                f"No handler for service '{service_name}'.",
                type=nexusrpc.HandlerErrorType.NOT_FOUND,
            )
        return service

    def _validate_all_operation_handlers_are_async(self) -> None:
        for service_handler in self._service_handlers.values():
            for op_handler in service_handler.operation_handlers.values():
                for method in [op_handler.start, op_handler.cancel]:
                    if not is_async_callable(method):
                        raise RuntimeError(
                            f"Operation handler method {method} is not an `async def` method, "
                            f"but you have not supplied an executor."
                        )

    async def start_operation(
        self,
        ctx: StartOperationContext,
        payload: temporalio.api.common.v1.Payload,
    ) -> StartOperationResultSync[Any] | StartOperationResultAsync:
        service_handler = self._get_service_handler(ctx.service)
        op_handler = service_handler.get_operation_handler(ctx.operation)
        op_defn = service_handler.service.operation_definitions[ctx.operation]

        deserialized_input = await self._deserialize_nexus_input(
            payload, op_defn.input_type
        )

        inbound = self._build_interceptor_chain(op_handler)
        return await inbound.execute_nexus_operation_start(
            ExecuteNexusOperationStartInput(ctx, deserialized_input)
        )

    async def cancel_operation(self, ctx: CancelOperationContext, token: str) -> None:
        service_handler = self._get_service_handler(ctx.service)
        op_handler = service_handler.get_operation_handler(ctx.operation)

        inbound = self._build_interceptor_chain(op_handler)
        return await inbound.execute_nexus_operation_cancel(
            ExecuteNexusOperationCancelInput(ctx, token)
        )

    def _build_interceptor_chain(
        self, op_handler: OperationHandler[Any, Any]
    ) -> NexusOperationInboundInterceptor:
        return reduce(
            lambda impl, interceptor: interceptor.intercept_nexus_operation(impl),
            reversed(self._interceptors),
            cast(
                NexusOperationInboundInterceptor,
                _NexusOperationInboundInterceptorImpl(op_handler, self._executor),
            ),
        )

    async def _deserialize_nexus_input(
        self,
        payload: temporalio.api.common.v1.Payload,
        input_type: type[Any] | None,
    ) -> Any:
        """Deserialize a Nexus operation input payload using the Temporal data converter.

        Applies the payload codec (if configured) and then the payload converter.
        """
        if self._data_converter.payload_codec:
            try:
                [payload] = await self._data_converter.payload_codec.decode([payload])
            except Exception as err:
                raise nexusrpc.HandlerError(
                    "Payload codec failed to decode Nexus operation input",
                    type=nexusrpc.HandlerErrorType.INTERNAL,
                ) from err

        try:
            [result] = self._data_converter.payload_converter.from_payloads(
                [payload],
                type_hints=[input_type] if input_type else None,
            )
            return result
        except Exception as err:
            raise nexusrpc.HandlerError(
                "Payload converter failed to decode Nexus operation input",
                type=nexusrpc.HandlerErrorType.BAD_REQUEST,
                retryable_override=False,
            ) from err
