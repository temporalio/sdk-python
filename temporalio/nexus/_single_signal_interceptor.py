from __future__ import annotations

import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import temporalio.client
from temporalio.nexus._operation_context import _operation_signal_context
from temporalio.nexus._temporal_client import _temporal_operation_signal_slot

_single_signal_clients: weakref.WeakKeyDictionary[
    temporalio.client.Client, temporalio.client.Client
] = weakref.WeakKeyDictionary()


def _single_signal_client(  # pyright: ignore[reportUnusedFunction]
    base: temporalio.client.Client,
) -> temporalio.client.Client:
    client = _single_signal_clients.get(base)
    if client is None:
        config = base.config()
        # Outermost, so a failure from any other interceptor frees the slot.
        config["interceptors"] = [_SingleSignalInterceptor(), *config["interceptors"]]
        client = temporalio.client.Client(**config)
        _single_signal_clients[base] = client
    return client


class _SingleSignalInterceptor(temporalio.client.Interceptor):
    def intercept_client(
        self, next: temporalio.client.OutboundInterceptor
    ) -> temporalio.client.OutboundInterceptor:
        return _SingleSignalOutboundInterceptor(next)


@contextmanager
def _reserve_signal() -> Iterator[None]:
    slot = _temporal_operation_signal_slot.get()
    if slot is None:
        yield
        return
    with slot.reserve(), _operation_signal_context():
        yield


class _SingleSignalOutboundInterceptor(temporalio.client.OutboundInterceptor):
    async def signal_workflow(
        self, input: temporalio.client.SignalWorkflowInput
    ) -> None:
        with _reserve_signal():
            await self.next.signal_workflow(input)

    async def start_workflow(
        self, input: temporalio.client.StartWorkflowInput
    ) -> temporalio.client.WorkflowHandle[Any, Any]:
        if input.start_signal is None:
            return await self.next.start_workflow(input)
        with _reserve_signal():
            return await self.next.start_workflow(input)
