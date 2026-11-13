"""The Worker side of :py:func:`temporalio.workflow.read_stream`."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import temporalio.api.stream.v1
import temporalio.common
import temporalio.workflow
from temporalio.client._stream_read import (
    _PENDING_STREAM_READS,
    _PendingStreamRead,
    _StreamReadMarker,
)
from temporalio.client._stream_store import (
    _STREAM_SERVICE,
    StreamConfig,
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


async def _read_stream_in_workflow(  # pyright: ignore[reportUnusedFunction]
    instance: _WorkflowInstanceImpl,
    config: StreamConfig,
    stream: Stream[Any],
    *,
    until: Callable[[StreamRecord[Any]], bool] | None,
    from_offset: bytes | None,
    max_records: int,
    timeout: timedelta,
) -> temporalio.workflow.StreamRead[Any]:
    request = ReadRecordsRequest(ref=stream.ref, from_offset=from_offset)
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
    pending = _PendingStreamRead(until=until, to_record=to_record)
    _PENDING_STREAM_READS.add(run_id, read_id, pending)
    try:
        with _store_errors():
            result = await temporalio.workflow.execute_local_activity(
                _store_read_activity_name(store.name),
                args=[
                    read_id,
                    request_message.SerializeToString(),
                    max_records,
                    timeout.total_seconds(),
                ],
                result_type=temporalio.common.RawValue,
                start_to_close_timeout=timeout + _READ_ACTIVITY_GRACE,
                retry_policy=config.workflow_call_options.retry_policy,
            )
        marker = _StreamReadMarker.from_payload(result.payload)
        if marker is None or marker.read_id != read_id:
            raise RuntimeError(
                f"The read of the Stream '{stream.ref.id}' got a result that is not "
                "its read marker"
            )
        records = pending.records_for(marker)
        if records is None:
            raise RuntimeError(
                f"The Worker has no Records for the read of {marker.describe_range()}"
            )
        return temporalio.workflow.StreamRead(
            records=[to_record(record) for record in records],
            last_offset=marker.last_offset,
            matched=marker.matched,
            done=marker.done,
        )
    finally:
        _PENDING_STREAM_READS.remove(run_id, read_id, pending)
