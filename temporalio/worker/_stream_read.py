"""The Workflow side of :py:func:`temporalio.workflow.take_stream`, ``filter_stream`` and
``reduce_stream``."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

import temporalio.api.stream.v1
import temporalio.api.streamservice.v1
import temporalio.common
import temporalio.workflow
from temporalio.client._stream_read import (
    _PENDING_STREAM_CALLS,
    _STREAM_CALL_ACTIVITY,
    _PendingStreamCall,
    _StreamCallResult,
)
from temporalio.client._stream_store import _STREAM_SERVICE, StreamConfig
from temporalio.nexus.system.stream_service.models import (
    ReadRecordsRequest,
    StreamRecord,
)
from temporalio.nexus.system.stream_service.workflow._resources.stream import Stream
from temporalio.workflow._streams import (
    StreamRead,
    StreamReduce,
    _StreamCall,
    _StreamCallKind,
)

from ._stream_store import _no_stream_config_error, _store_errors, _workflow_target

if TYPE_CHECKING:
    from ._workflow_instance import _WorkflowInstanceImpl

# The Local Activity gets this much time after the call's own timeout to give its result.
_STREAM_CALL_GRACE = timedelta(seconds=10)


def _describe(
    kind: _StreamCallKind, store: str | None, stream_id: str, from_offset: bytes | None
) -> str:
    start = from_offset.hex() if from_offset else "the beginning"
    on_store = f" on the Store '{store}'" if store else ""
    return f"a {kind.value} of the Stream '{stream_id}'{on_store} after {start}"


async def _stream_call_in_workflow(  # pyright: ignore[reportUnusedFunction]
    instance: _WorkflowInstanceImpl,
    config: StreamConfig | None,
    stream: Any,
    call: _StreamCall,
    call_id: str,
) -> StreamRead[Any] | StreamReduce[Any]:
    """Run a call of Workflow code in one Local Activity, or take its result from History."""
    if not isinstance(stream, Stream):
        raise TypeError(
            "Workflow code takes a Workflow Stream, from temporalio.workflow.get_stream "
            "or create_stream"
        )
    if config is None and not instance._is_replaying:
        raise _no_stream_config_error()
    request = ReadRecordsRequest(ref=stream.ref, from_offset=call.from_offset)
    payload_converter, _ = instance._system_nexus_converters_for(
        _STREAM_SERVICE.name, "ReadRecords", request
    )
    message = payload_converter._to_transfer_value(request)
    assert isinstance(message, temporalio.api.streamservice.v1.ReadRecordsRequest)
    record_type: Any = StreamRecord[stream.body_type]  # type: ignore[name-defined]

    def to_record(record: temporalio.api.stream.v1.StreamRecord) -> Any:
        return payload_converter._from_transfer_value(record, record_type)

    # A replay takes the result from History, so it needs no Store and no config. The
    # choice of a Store runs in Workflow code, as it does for the other Stream calls.
    store = (
        config._store_for(stream.ref, _workflow_target())
        if config is not None
        else None
    )
    store_name = store.name if store is not None else stream.ref.store
    run_id = temporalio.workflow.info().run_id
    pending = None
    if store is not None:
        pending = _PendingStreamCall(
            call=call, store=store, ref=message.ref, to_record=to_record
        )
        _PENDING_STREAM_CALLS.add(run_id, call_id, pending)
    try:
        with _store_errors():
            raw = await temporalio.workflow.execute_local_activity(
                _STREAM_CALL_ACTIVITY,
                call_id,
                result_type=temporalio.common.RawValue,
                start_to_close_timeout=call.timeout + _STREAM_CALL_GRACE,
                retry_policy=config.workflow_call_options.retry_policy
                if config is not None
                else None,
            )
    finally:
        if pending is not None:
            _PENDING_STREAM_CALLS.remove(run_id, call_id, pending)
    result = _StreamCallResult.from_payload(raw.payload)
    if (
        result is None
        or result.kind is not call.kind
        or result.stream_id != message.ref.id
        or (store_name and result.store != store_name)
        or result.from_offset != call.from_offset
    ):
        recorded = (
            "no Stream call"
            if result is None
            else _describe(
                result.kind, result.store, result.stream_id, result.from_offset
            )
        )
        raise temporalio.workflow.NondeterminismError(
            f"Stream call {call_id} of the Workflow is "
            f"{_describe(call.kind, store_name, message.ref.id, call.from_offset)}, "
            f"and History has {recorded}"
        )
    if call.kind is _StreamCallKind.REDUCE:
        assert result.value is not None
        return StreamReduce(
            value=instance.workflow_payload_converter().from_payload(
                result.value, call.value_type
            ),
            count=result.count,
            last_offset=result.last_offset,
            stop=result.stop,
            done=result.done,
        )
    return StreamRead(
        records=[to_record(record) for record in result.records],
        last_offset=result.last_offset,
        stop=result.stop,
        done=result.done,
    )
