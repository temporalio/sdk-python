"""The Worker side of :py:func:`temporalio.workflow.read_stream` and ``reduce_stream``.

Workflow code starts the read here, and the Worker gives a replayed read its Records.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import temporalio.api.stream.v1
import temporalio.bridge.proto.workflow_activation
import temporalio.common
import temporalio.converter
import temporalio.exceptions
import temporalio.workflow
from temporalio.client._stream_read import (
    _PENDING_STREAM_READS,
    _PendingStreamRead,
    _read_range,
    _StreamFold,
    _StreamReadMarker,
)
from temporalio.client._stream_store import (
    _STREAM_SERVICE,
    StreamConfig,
    _store_page_reader,
    _store_read_activity_name,
)
from temporalio.nexus.system.stream_service.models import (
    ReadRecordsRequest,
    StreamRecord,
)
from temporalio.nexus.system.stream_service.workflow._resources.stream import Stream

from ._stream_store import _store_errors, _workflow_target

if TYPE_CHECKING:
    from ._workflow_instance import _WorkflowInstanceImpl

# The read Activity gets this much time after the read's own timeout to give its result.
_READ_ACTIVITY_GRACE = timedelta(seconds=10)


async def _run_read(
    instance: _WorkflowInstanceImpl,
    config: StreamConfig,
    stream: Stream[Any],
    limits: temporalio.workflow._StreamReadLimits,
    *,
    until: Callable[..., bool] | None,
    fold: _StreamFold | None,
) -> tuple[
    _StreamReadMarker,
    list[temporalio.api.stream.v1.StreamRecord],
    Callable[[temporalio.api.stream.v1.StreamRecord], Any],
]:
    """Run the read Activity, and give its marker, its proto Records and their typing."""
    request = ReadRecordsRequest(ref=stream.ref, from_offset=limits.from_offset)
    payload_converter, _ = instance._system_nexus_converters_for(
        _STREAM_SERVICE.name, "ReadRecords", request
    )
    request_message = payload_converter._to_transfer_value(request)
    # The choice runs in Workflow code, as it does for the other Stream calls.
    store = config._store_for(stream.ref, _workflow_target())
    record_type: Any = StreamRecord[stream.body_type]  # type: ignore[name-defined]

    def to_record(record: temporalio.api.stream.v1.StreamRecord) -> Any:
        return payload_converter._from_transfer_value(record, record_type)

    run_id = temporalio.workflow.info().run_id
    read_id = str(temporalio.workflow.uuid4())
    pending = _PendingStreamRead(to_record=to_record, until=until, fold=fold)
    _PENDING_STREAM_READS.add(run_id, read_id, pending)
    try:
        with _store_errors():
            result = await temporalio.workflow.execute_local_activity(
                _store_read_activity_name(store.name),
                args=[
                    read_id,
                    request_message.SerializeToString(),
                    limits.max_records,
                    limits.max_bytes,
                    None
                    if limits.idle_timeout is None
                    else limits.idle_timeout.total_seconds(),
                    limits.timeout.total_seconds(),
                ],
                result_type=temporalio.common.RawValue,
                start_to_close_timeout=limits.timeout + _READ_ACTIVITY_GRACE,
                retry_policy=config.workflow_call_options.retry_policy,
            )
        marker = _StreamReadMarker.from_payload(result.payload)
        if marker is None or marker.read_id != read_id:
            raise RuntimeError(
                f"The read of the Stream '{stream.ref.id}' got a result that is not "
                "its read marker"
            )
        if fold is not None:
            return marker, [], to_record
        records = pending.records_for(marker)
        if records is None:
            raise RuntimeError(
                f"The Worker has no Records for the read of {marker.describe_range()}"
            )
        return marker, records, to_record
    finally:
        _PENDING_STREAM_READS.remove(run_id, read_id, pending)


async def _read_stream_in_workflow(  # pyright: ignore[reportUnusedFunction]
    instance: _WorkflowInstanceImpl,
    config: StreamConfig,
    stream: Stream[Any],
    until: Callable[[StreamRecord[Any]], bool] | None,
    limits: temporalio.workflow._StreamReadLimits,
) -> temporalio.workflow.StreamRead[Any]:
    marker, records, to_record = await _run_read(
        instance, config, stream, limits, until=until, fold=None
    )
    return temporalio.workflow.StreamRead(
        records=[to_record(record) for record in records],
        last_offset=marker.last_offset,
        stop=marker.stop,
        done=marker.done,
    )


async def _reduce_stream_in_workflow(  # pyright: ignore[reportUnusedFunction]
    instance: _WorkflowInstanceImpl,
    config: StreamConfig,
    stream: Stream[Any],
    fold: _StreamFold,
    until: Callable[[Any, StreamRecord[Any]], bool] | None,
    value_type: Any,
    limits: temporalio.workflow._StreamReadLimits,
) -> temporalio.workflow.StreamReduce[Any]:
    marker, _, _ = await _run_read(
        instance, config, stream, limits, until=until, fold=fold
    )
    if marker.value is None:
        raise RuntimeError(
            f"The fold of {marker.describe_range()} got a marker with no value"
        )
    # A first run and a replay both take the value from the marker.
    value = instance.workflow_payload_converter().from_payload(marker.value, value_type)
    return temporalio.workflow.StreamReduce(
        value=value,
        count=marker.record_count,
        last_offset=marker.last_offset,
        stop=marker.stop,
        done=marker.done,
    )


async def _fill_stream_reads(  # pyright: ignore[reportUnusedFunction]
    act: temporalio.bridge.proto.workflow_activation.WorkflowActivation,
    config: StreamConfig,
    data_converter: temporalio.converter.DataConverter,
) -> None:
    """Give each read that this activation resolves its Records, before the Workflow runs.

    History has only the marker of a read. On a replay, the Records come from the Store
    again, through the same codec as the read Activity used.

    Raises:
        RuntimeError: The Store failed, the Records are gone, or they don't match the
            marker. The activation fails, and so does the Workflow Task.
    """
    for job in act.jobs:
        if not job.HasField("resolve_activity"):
            continue
        resolve = job.resolve_activity
        if not resolve.is_local or not resolve.result.HasField("completed"):
            continue
        marker = _StreamReadMarker.from_payload(resolve.result.completed.result)
        # History has the value of a fold, so its replay needs no Store.
        if marker is None or marker.value is not None:
            continue
        pending = _PENDING_STREAM_READS.get(act.run_id, marker.read_id)
        # On a first run the read Activity already gave its Records.
        if pending is None or pending.records_for(marker) is not None:
            continue
        try:
            store = config._get_store_by_name(marker.store)
        except temporalio.exceptions.StreamConfigError as err:
            raise RuntimeError(
                f"The replay of a Workflow read can't read {marker.describe_range()}: "
                f"{err}"
            ) from err
        page_reader = _store_page_reader(store, data_converter)
        pending.fill(marker, await _read_range(page_reader, marker))
