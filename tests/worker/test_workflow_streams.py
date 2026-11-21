"""Tests of the Stream calls of one Workflow instance, with no server."""

import dataclasses
from datetime import datetime, timedelta, timezone

import temporalio.api.common.v1
import temporalio.bridge.worker
import temporalio.common
import temporalio.converter
import temporalio.converter._extstore
import temporalio.nexus.system
import temporalio.worker
from temporalio import workflow
from temporalio.api.common.v1 import Payload
from temporalio.api.failure.v1 import ApplicationFailureInfo, Failure
from temporalio.api.sdk.v1.external_storage_pb2 import ExternalStorageReference
from temporalio.api.stream.v1 import StreamRecord, StreamRef
from temporalio.bridge.proto.activity_result import ActivityResolution
from temporalio.bridge.proto.activity_result import Success as ActivitySuccess
from temporalio.bridge.proto.workflow_activation import (
    ResolveActivity,
    ResolveStreamAppend,
    ResolveStreamClose,
    ResolveStreamRead,
    StreamPage,
    WorkflowActivation,
    WorkflowActivationJob,
)
from temporalio.bridge.proto.workflow_commands import (
    AppendToStream,
    CompleteWorkflowExecution,
    WorkflowCommand,
)
from temporalio.bridge.proto.workflow_completion import (
    Success,
    WorkflowActivationCompletion,
)
from temporalio.converter import ExternalStorage, WorkflowSerializationContext
from temporalio.worker._workflow import _CommandAwareDataConverter
from tests.helpers.streams import CodecCall, ContextCodec, wrapped
from tests.test_extstore import InMemoryTestDriver


@workflow.defn
class _StreamWorkflow:
    @workflow.run
    async def run(self) -> None:
        pass


def _new_instance(workflow_class: type) -> temporalio.worker.WorkflowInstance:
    defn = workflow._Definition.must_from_class(workflow_class)
    start_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return temporalio.worker.UnsandboxedWorkflowRunner().create_instance(
        temporalio.worker.WorkflowInstanceDetails(
            payload_converter_factory=temporalio.converter.DefaultPayloadConverter,
            failure_converter_class=temporalio.converter.DefaultFailureConverter,
            interceptor_classes=[],
            defn=defn,
            info=workflow.Info(
                attempt=1,
                continued_run_id=None,
                cron_schedule=None,
                execution_timeout=None,
                first_execution_run_id="first-run",
                headers={},
                namespace="ns",
                original_execution_run_id="run",
                parent=None,
                root=None,
                priority=temporalio.common.Priority(),
                raw_memo={},
                retry_policy=None,
                run_id="run",
                run_timeout=None,
                search_attributes={},
                start_time=start_time,
                task_queue="tq",
                task_timeout=timedelta(seconds=10),
                typed_search_attributes=temporalio.common.TypedSearchAttributes.empty,
                workflow_id="wf",
                workflow_start_time=start_time,
                workflow_type=defn.name or "",
            ),
            randomness_seed=1,
            extern_functions={},
            disable_eager_activity_execution=False,
            worker_level_failure_exception_types=[],
            patch_activation_callback=None,
            last_completion_result=temporalio.api.common.v1.Payloads(),
            last_failure=None,
        )
    )


async def test_stream_payloads_take_the_codec_with_no_context_and_no_storage() -> None:
    calls: list[CodecCall] = []
    driver = InMemoryTestDriver()
    converter = dataclasses.replace(
        temporalio.converter.DataConverter.default,
        payload_codec=ContextCodec(calls),
        external_storage=ExternalStorage(drivers=[driver], payload_size_threshold=0),
    )
    context = WorkflowSerializationContext(namespace="ns", workflow_id="wf")
    command_aware = _CommandAwareDataConverter.create(
        instance=_new_instance(_StreamWorkflow),
        context_free_dc=converter,
        workflow_context_dc=converter.with_context(context),
        workflow_context=context,
    )
    to_payload = temporalio.converter.PayloadConverter.default.to_payload
    reference = to_payload(ExternalStorageReference(driver_name=driver.name()))
    bodies = [to_payload("plain"), reference]
    result = to_payload("result")

    completion = WorkflowActivationCompletion(
        successful=Success(
            commands=[
                WorkflowCommand(
                    append_to_stream=AppendToStream(
                        seq=1, stream=StreamRef(id="s", store="memory"), bodies=bodies
                    )
                ),
                WorkflowCommand(
                    complete_workflow_execution=CompleteWorkflowExecution(result=result)
                ),
            ]
        )
    )
    await temporalio.bridge.worker.encode_completion(
        completion, command_aware, encode_headers=True, storage_concurrency_limit=1
    )
    append, complete = completion.successful.commands
    assert list(append.append_to_stream.bodies) == [wrapped(body) for body in bodies]
    stored = complete.complete_workflow_execution.result
    assert stored.metadata["messageType"] == reference.metadata["messageType"]
    assert driver._store_calls == 1
    assert calls == [("encode", None, 1)] * 2 + [("encode", context, 1)]

    # A writer with no codec can write a claim of a payload in this Worker's storage, and
    # a body that has the mark of a system Nexus envelope.
    claim = Payload()
    claim.CopyFrom(stored)
    claim.metadata[temporalio.converter._extstore._REFERENCE_MARKER_KEY] = b"true"
    envelope = to_payload("envelope")
    envelope.metadata[temporalio.nexus.system._SYSTEM_PAYLOAD_METADATA_KEY] = b"true"
    failure = Failure(
        message="The Stream is closed",
        application_failure_info=ApplicationFailureInfo(
            type="stream_closed",
            details=temporalio.api.common.v1.Payloads(payloads=[wrapped(result)]),
        ),
    )
    records = [*append.append_to_stream.bodies, claim, envelope]
    activation = WorkflowActivation(
        jobs=[
            WorkflowActivationJob(
                resolve_stream_read=ResolveStreamRead(
                    seq=1,
                    page=StreamPage(
                        records=[
                            StreamRecord(body=body, offset=bytes([at + 1]))
                            for at, body in enumerate(records)
                        ]
                    ),
                )
            ),
            WorkflowActivationJob(
                resolve_stream_read=ResolveStreamRead(seq=2, failed=failure)
            ),
            WorkflowActivationJob(
                resolve_stream_append=ResolveStreamAppend(seq=1, failed=failure)
            ),
            WorkflowActivationJob(
                resolve_stream_close=ResolveStreamClose(seq=1, failed=failure)
            ),
            WorkflowActivationJob(
                resolve_activity=ResolveActivity(
                    seq=1,
                    result=ActivityResolution(completed=ActivitySuccess(result=stored)),
                )
            ),
        ]
    )
    calls.clear()
    await temporalio.bridge.worker.decode_activation(
        activation, command_aware, decode_headers=True, storage_concurrency_limit=1
    )
    read, failed_read, appended, closed, activity = activation.jobs
    assert [record.body for record in read.resolve_stream_read.page.records] == [
        *bodies,
        claim,
        envelope,
    ]
    assert failed_read.resolve_stream_read.failed == failure
    assert appended.resolve_stream_append.failed == failure
    assert closed.resolve_stream_close.failed == failure
    assert activity.resolve_activity.result.completed.result == result
    assert driver._retrieve_calls == 1
    assert calls == [("decode", None, 1)] * 4 + [("decode", context, 1)]
