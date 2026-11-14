"""The Workflow side of the take, filter and reduce calls of a Stream.

Workflow code runs a call here, and the Worker reads the Records of a replayed call from the
Store when its marker in History has only their offsets.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import temporalio.api.stream.v1
import temporalio.api.streamservice.v1
import temporalio.bridge.proto.workflow_activation
import temporalio.common
import temporalio.converter
import temporalio.exceptions
import temporalio.workflow
from temporalio.client._stream_read import (
    _PENDING_STREAM_CALLS,
    _STREAM_CALL_ACTIVITY,
    _PendingStreamCall,
    _read_kept_records,
    _replay_error,
    _StreamCallResult,
)
from temporalio.client._stream_store import (
    _STREAM_SERVICE,
    StreamConfig,
    StreamReplaySource,
    _store_page_reader,
)
from temporalio.nexus.system._support import _StreamCall, _StreamCallKind
from temporalio.nexus.system.stream_service.models import (
    ReadRecordsRequest,
    StreamRead,
    StreamRecord,
    StreamReduce,
)
from temporalio.nexus.system.stream_service.workflow._resources.stream import Stream

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

    # A replay of a marker with Records needs no Store and no config. The choice of a
    # Store runs in Workflow code, as it does for the other Stream calls.
    store = (
        config._store_for(stream.ref, _workflow_target())
        if config is not None
        else None
    )
    store_name = store.name if store is not None else stream.ref.store
    run_id = temporalio.workflow.info().run_id
    pending = None
    if store is not None:
        assert config is not None
        replay_from = config.workflow_read_replay_from
        if (
            replay_from is StreamReplaySource.STORE
            and call.kind is not _StreamCallKind.REDUCE
            and call.default_max_bytes
        ):
            # No body goes into History, so the default cap that keeps a marker small
            # has no use here.
            call = dataclasses.replace(call, max_bytes=None)
        pending = _PendingStreamCall(
            call=call,
            store=store,
            ref=message.ref,
            to_record=to_record,
            replay_from=replay_from,
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
        or (result.replay is not None and result.replay.call_id != call_id)
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
    records = result.records
    if result.replay is not None:
        # The Worker read these Records before the activation, or the Local Activity of
        # the first run kept them.
        filled = pending.records_for(result) if pending is not None else None
        if filled is None:
            raise _replay_error(
                result,
                "the Worker has no Stream config"
                if config is None
                else "the Worker read no Records for this call",
            )
        records = filled
    return StreamRead(
        records=[to_record(record) for record in records],
        last_offset=result.last_offset,
        stop=result.stop,
        done=result.done,
    )


async def _read_store_records(  # pyright: ignore[reportUnusedFunction]
    act: temporalio.bridge.proto.workflow_activation.WorkflowActivation,
    config: StreamConfig | None,
    data_converter: temporalio.converter.DataConverter,
) -> None:
    """Read the Records of each marker without Records in this activation.

    It runs before the Workflow sees the activation. The read goes through the same codec
    as the Local Activity of the first run. It runs for every such marker, whatever the
    Worker's ``workflow_read_replay_from`` is.

    Raises:
        temporalio.exceptions.StreamReplayError: The Worker has no Stream config, the
            Store failed, or the Records are gone or differ. The Workflow Task fails.
    """
    for job in act.jobs:
        if not job.HasField("resolve_activity"):
            continue
        resolve = job.resolve_activity
        if not resolve.is_local or not resolve.result.HasField("completed"):
            continue
        result = _StreamCallResult.from_payload(resolve.result.completed.result)
        if result is None or result.replay is None:
            continue
        if config is None:
            raise _replay_error(result, "the Worker has no Stream config")
        pending = _PENDING_STREAM_CALLS.get(act.run_id, result.replay.call_id)
        # Without a pending call, the Workflow code makes no such call, and its own check
        # reports that. A first run already has the Records of its Local Activity.
        if pending is None or pending.records_for(result) is not None:
            continue
        try:
            store = config._get_store_by_name(result.store)
        except temporalio.exceptions.StreamConfigError as err:
            raise _replay_error(result, str(err)) from err
        records = await _read_kept_records(
            _store_page_reader(store, data_converter), result
        )
        pending.fill(result, records)
