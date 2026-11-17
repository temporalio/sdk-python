"""How Workflow code reaches the Stream Stores of its Worker's client."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Generator
from typing import TYPE_CHECKING, Any

from nexusrpc import OutputT

import temporalio.exceptions
import temporalio.workflow
from temporalio.client._stream_store import (
    _STREAM_OPERATIONS,
    StreamConfig,
    _request_ref,
)
from temporalio.converter import StorageDriverWorkflowInfo

from ._interceptor import StartNexusOperationInput

if TYPE_CHECKING:
    from ._workflow_instance import _WorkflowInstanceImpl


def _no_stream_config_error() -> temporalio.exceptions.ApplicationError:  # pyright: ignore[reportUnusedFunction]
    # A `StreamConfigError` would fail the Workflow Task, and its retries would fail the
    # same way for good. A Worker with no config is a setup that a user has to see, so
    # the Workflow fails instead.
    return temporalio.exceptions.ApplicationError(
        "The client of this Worker has no Stream Store. Give one in "
        "stream_config=StreamConfig(stores=[...]).",
        type="StreamConfigError",
        non_retryable=True,
    )


# The writes of a Workflow to one Stream, which run one at a time in the order of the
# calls. A create and a describe need no order.
_ORDERED_OPERATIONS = frozenset({"AppendRecords", "CloseStream"})


def _workflow_target() -> StorageDriverWorkflowInfo:
    # External storage builds the same value for a Workflow.
    info = temporalio.workflow.info()
    return StorageDriverWorkflowInfo(
        id=info.workflow_id,
        type=info.workflow_type,
        run_id=info.run_id,
        namespace=info.namespace,
    )


@dataclasses.dataclass(frozen=True)
class _WorkflowStreamStore:
    """The Worker's Stream Stores as Workflow code reaches them."""

    config: StreamConfig
    """The Stream config of the Worker's client."""

    @classmethod
    def from_config(cls, config: StreamConfig | None) -> _WorkflowStreamStore | None:
        """Build it from the Stream config of a client, or give ``None`` when it has none."""
        return cls(config=config) if config is not None else None

    def start_operation(
        self,
        instance: _WorkflowInstanceImpl,
        input: StartNexusOperationInput[Any, OutputT],
    ) -> temporalio.workflow.NexusOperationHandle[OutputT]:
        """Answer a Stream call of Workflow code with the Store that the call goes to."""
        stream_operation = _STREAM_OPERATIONS[input.operation_name]
        # Workflow code can start any operation of the generated service through a Nexus
        # client, also one that the Workflow `Stream` leaves out.
        if stream_operation.workflow_refusal is not None:
            raise temporalio.exceptions.ApplicationError(
                stream_operation.workflow_refusal, non_retryable=True
            )
        payload_converter, _ = instance._system_nexus_converters(input)
        request = payload_converter._to_transfer_value(input.input)
        ref = _request_ref(input.input)
        # The choice runs in Workflow code, so a ref of a Store that the config doesn't
        # have raises `StreamConfigError` here, with no I/O, and fails the Workflow Task.
        store = self.config._store_for(ref, _workflow_target())
        options = self.config.workflow_call_options
        tails = instance._stream_write_tails
        tail_key = (store.name, ref.id)
        previous = (
            tails.get(tail_key) if input.operation_name in _ORDERED_OPERATIONS else None
        )

        async def result() -> OutputT:
            if previous is not None and not previous.done():
                # The Workflow's appends carry sequences that go up in the order of the
                # calls, and the Store refuses one below the last. So a write starts its
                # Local Activity only after the one before it ended, also when that one
                # failed.
                await temporalio.workflow.wait([previous])
            try:
                response = await store.call_from_workflow(
                    input.operation_name, request, options
                )
            except temporalio.exceptions.ApplicationError as err:
                # Core hands back a failed Local Activity's error without a wrapper.
                stream_error = temporalio.exceptions._stream_error_from_failure(err)
                if stream_error is not None:
                    raise stream_error from None
                raise
            except temporalio.exceptions.ActivityError as err:
                # Workflow code gets the Store's error, not the Local Activity's wrapper.
                if err.cause is not None:
                    stream_error = temporalio.exceptions._stream_error_from_failure(
                        err.cause
                    )
                    if stream_error is not None:
                        raise stream_error from None
                if isinstance(
                    err.cause,
                    (
                        temporalio.exceptions.ApplicationError,
                        temporalio.exceptions.TimeoutError,
                    ),
                ):
                    raise err.cause
                raise
            return payload_converter._from_transfer_value(
                stream_operation.check_response(store, response), input.output_type
            )

        task = instance.create_task(
            result(), name=f"stream store: {input.operation_name}"
        )
        if input.operation_name in _ORDERED_OPERATIONS:
            tails[tail_key] = task

            def forget(done: asyncio.Task[Any]) -> None:
                if tails.get(tail_key) is done:
                    del tails[tail_key]

            task.add_done_callback(forget)
        return _StreamStoreOperationHandle(task)


class _StreamStoreOperationHandle(temporalio.workflow.NexusOperationHandle[OutputT]):
    def __init__(self, task: asyncio.Task[OutputT]) -> None:
        self._task = task

    def cancel(self) -> bool:
        return self._task.cancel()

    def __await__(self) -> Generator[Any, Any, OutputT]:
        return self._task.__await__()

    @property
    def operation_token(self) -> str | None:
        return None
