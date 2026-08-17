"""Event Groups tests, following Workspace/test-plan.md.

The existing ``test_event_groups.py`` is the pre-plan smoke suite and is left
in place until this file replaces it. Case IDs in comments are the plan's;
TypeScript's suite is a style reference only and may still change.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Sequence
from datetime import timedelta

import nexusrpc
import pytest

from temporalio import activity, workflow
from temporalio.api.common.v1 import Payload, WorkflowExecution
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from temporalio.api.sdk.v1 import EventGroupMarker
from temporalio.api.workflowservice.v1 import ResetWorkflowExecutionRequest
from temporalio.client import Client, WorkflowHandle
from temporalio.common import RawValue, RetryPolicy, SearchAttributeKey
from temporalio.converter import (
    CompositePayloadConverter,
    DataConverter,
    DefaultPayloadConverter,
    EncodingPayloadConverter,
    PayloadCodec,
    PayloadConverter,
)
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    ChildWorkflowError,
    NexusOperationError,
    TemporalError,
)
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers import assert_eventually, ensure_search_attributes_present, new_worker
from tests.helpers.nexus import make_nexus_endpoint_name

# These tests need a server that transcribes Event Group markers onto history.
# Time-skipping (the Java test server) does not.

_ACT_TIMEOUT = timedelta(seconds=10)


def _require_event_groups_server(env: WorkflowEnvironment) -> None:
    if env.supports_time_skipping:
        pytest.skip("Event Groups require a server that transcribes markers")


####################################################################################################
# 1. Explicit Event Groups Marker Label IDs (`EG-LABEL-ID`)
####################################################################################################


@workflow.defn
class DerivedIdsWorkflow:
    @workflow.run
    async def run(self) -> None:
        a = workflow.create_event_group("aaa")
        b1 = workflow.create_event_group("bbb")
        b2 = workflow.create_event_group("bbb")
        await _activity("activity-a", [a])
        await _activity("activity-b1", [b1])
        await _activity("activity-b2", [b2])


async def test_derived_label_ids(client: Client, env: WorkflowEnvironment):
    _require_event_groups_server(env)

    async with new_worker(
        client, DerivedIdsWorkflow, activities=[noop_activity]
    ) as worker:
        handle1 = await client.start_workflow(
            DerivedIdsWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        handle2 = await client.start_workflow(
            DerivedIdsWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle1.result()
        await handle2.result()
        events1 = await _fetch_events(handle1)
        events2 = await _fetch_events(handle2)
        run_id1 = _run_id(handle1)

        assert (
            len(_events_of_type(events1, EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED))
            == 3
        )
        assert (
            len(_events_of_type(events2, EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED))
            == 3
        )

        # EG-LABEL-ID-00: Derived IDs match SHA1(original_execution_run_id + label)
        _assert_marker_ids(
            _activity_event(events1, "activity-a"),
            _label_marker_id(_default_marker_id(run_id1, "aaa")),
        )

        # EG-LABEL-ID-01: same label + no user-provided ID => same group
        assert _marker_ids(_activity_event(events1, "activity-b1")) == _marker_ids(
            _activity_event(events1, "activity-b2")
        )

        # EG-LABEL-ID-02: different labels + no user-provided ID => distinct groups
        assert _marker_ids(_activity_event(events1, "activity-a")) != _marker_ids(
            _activity_event(events1, "activity-b1")
        )

        # EG-LABEL-ID-03: same labels + different workflow execs => distinct groups
        assert _marker_ids(_activity_event(events1, "activity-a")) != _marker_ids(
            _activity_event(events2, "activity-a")
        )


async def test_derived_label_ids_stable_across_reset(
    client: Client, env: WorkflowEnvironment
):
    _require_event_groups_server(env)

    async with new_worker(
        client, DerivedIdsWorkflow, activities=[noop_activity]
    ) as worker:
        handle = await client.start_workflow(
            DerivedIdsWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()
        events = await _fetch_events(handle)
        original_run_id = _run_id(handle)

        first_wft_started = next(
            e.event_id
            for e in events
            if e.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_STARTED
        )
        reset = await client.workflow_service.reset_workflow_execution(
            ResetWorkflowExecutionRequest(
                namespace=client.namespace,
                workflow_execution=WorkflowExecution(
                    workflow_id=handle.id, run_id=original_run_id
                ),
                reason="test event group id stability across reset",
                request_id=str(uuid.uuid4()),
                workflow_task_finish_event_id=first_wft_started,
            )
        )
        assert reset.run_id != original_run_id
        reset_handle = client.get_workflow_handle(handle.id, run_id=reset.run_id)
        await reset_handle.result()
        reset_events = await _fetch_events(reset_handle)

        assert (
            len(_events_of_type(events, EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED))
            == 3
        )
        assert (
            len(
                _events_of_type(
                    reset_events, EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
                )
            )
            == 3
        )

        # Control: reset re-executed the first workflow task
        assert (
            _events_of_type(events, EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED)[
                0
            ].event_time
            != _events_of_type(
                reset_events, EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED
            )[0].event_time
        )

        # EG-LABEL-ID-04: derived IDs are based on the original execution run id
        _assert_marker_ids(
            _activity_event(reset_events, "activity-a"),
            _label_marker_id(_default_marker_id(original_run_id, "aaa")),
        )
        assert _marker_ids(_activity_event(events, "activity-b1")) == _marker_ids(
            _activity_event(reset_events, "activity-b1")
        )


@workflow.defn
class UserProvidedIdsWorkflow:
    @workflow.run
    async def run(self) -> None:
        c = workflow.create_event_group("ccc", id="c-id")
        d1 = workflow.create_event_group("ddd1", id="d-id")
        d2 = workflow.create_event_group("ddd2", id="d-id")
        await _activity("activity-c", [c])
        await _activity("activity-d1", [d1])
        await _activity("activity-d2", [d2])


async def test_user_provided_label_ids(client: Client, env: WorkflowEnvironment):
    _require_event_groups_server(env)

    async with new_worker(
        client, UserProvidedIdsWorkflow, activities=[noop_activity]
    ) as worker:
        handle = await client.start_workflow(
            UserProvidedIdsWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()
        events = await _fetch_events(handle)
        assert (
            len(_events_of_type(events, EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED))
            == 3
        )

        # EG-LABEL-ID-20: user-provided IDs are used verbatim
        _assert_marker_ids(
            _activity_event(events, "activity-c"), _label_marker_id("c-id")
        )

        # EG-LABEL-ID-21: different labels + same user-provided ID => same group
        assert _marker_ids(_activity_event(events, "activity-d1")) == _marker_ids(
            _activity_event(events, "activity-d2")
        )


####################################################################################################
# 2. Explicit Event Groups Marker Label Payload (`EG-LABEL-PAYLOAD`)
####################################################################################################


@workflow.defn
class LabelPayloadWorkflow:
    @workflow.run
    async def run(self) -> None:
        a = workflow.create_event_group("aaa")
        b = workflow.create_event_group("bbb", id="b-id")
        # Control: activity arguments go through the worker's payload converter, so this is how the
        # custom-converter test proves that converter is actually installed.
        await workflow.execute_activity(
            control_activity,
            "control",
            start_to_close_timeout=_ACT_TIMEOUT,
            activity_id="control",
        )
        await _activity("activity-a", [a])
        await _activity("activity-b", [b])


class _CustomStringConverter(EncodingPayloadConverter):
    @property
    def encoding(self) -> str:
        return "custom"

    def to_payload(self, value: object) -> Payload | None:
        if isinstance(value, str):
            return Payload(
                metadata={"encoding": b"custom"},
                data=f"custom-converter-{value}".encode(),
            )
        return None

    def from_payload(self, payload: Payload, type_hint: type | None = None) -> str:
        text = payload.data.decode()
        prefix = "custom-converter-"
        return text[len(prefix) :] if text.startswith(prefix) else text


class _CustomPayloadConverter(CompositePayloadConverter):
    def __init__(self) -> None:
        super().__init__(
            _CustomStringConverter(),
            *DefaultPayloadConverter.default_encoding_payload_converters,
        )


class _WrappingPayloadCodec(PayloadCodec):
    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return [
            Payload(
                metadata={"encoding": b"binary/wrapped"}, data=p.SerializeToString()
            )
            for p in payloads
        ]

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        decoded: list[Payload] = []
        for payload in payloads:
            inner = Payload()
            inner.ParseFromString(payload.data)
            decoded.append(inner)
        return decoded


async def test_label_payload_is_json_plain(client: Client, env: WorkflowEnvironment):
    _require_event_groups_server(env)

    async with new_worker(
        client,
        LabelPayloadWorkflow,
        activities=[noop_activity, control_activity],
    ) as worker:
        handle = await client.start_workflow(
            LabelPayloadWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()
        events = await _fetch_events(handle)
        run_id = _run_id(handle)
        a_id = _default_marker_id(run_id, "aaa")

        activity_a = _activity_event(events, "activity-a")
        activity_b = _activity_event(events, "activity-b")
        _assert_markers(activity_a, _label_marker(a_id, "aaa"))
        _assert_markers(activity_b, _label_marker("b-id", "bbb"))

        # EG-LABEL-PAYLOAD-00: label payload is a json/plain JSON string
        assert _label_payload_of(activity_a, a_id) == ("json/plain", '"aaa"')
        assert _label_payload_of(activity_b, "b-id") == ("json/plain", '"bbb"')


async def test_label_payload_uses_default_converter_not_worker_converter(
    env: WorkflowEnvironment,
):
    _require_event_groups_server(env)

    custom_client = await env.connect_client(
        data_converter=DataConverter(payload_converter_class=_CustomPayloadConverter)
    )
    async with new_worker(
        custom_client,
        LabelPayloadWorkflow,
        activities=[noop_activity, control_activity],
    ) as worker:
        handle = await custom_client.start_workflow(
            LabelPayloadWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()
        events = await _fetch_events(handle)
        run_id = _run_id(handle)
        a_id = _default_marker_id(run_id, "aaa")

        control = _activity_event(events, "control")
        control_payload = (
            control.activity_task_scheduled_event_attributes.input.payloads[0]
        )
        assert control_payload.metadata["encoding"] == b"custom"
        assert control_payload.data == b"custom-converter-control"

        activity_a = _activity_event(events, "activity-a")
        activity_b = _activity_event(events, "activity-b")

        # EG-LABEL-PAYLOAD-01: labels still go through the SDK default converter
        assert _label_payload_of(activity_a, a_id) == ("json/plain", '"aaa"')
        assert _label_payload_of(activity_b, "b-id") == ("json/plain", '"bbb"')


async def test_label_payload_is_codec_encoded_but_ids_are_not(
    env: WorkflowEnvironment,
):
    _require_event_groups_server(env)

    codec = _WrappingPayloadCodec()
    codec_client = await env.connect_client(
        data_converter=DataConverter(payload_codec=codec)
    )
    async with new_worker(
        codec_client,
        LabelPayloadWorkflow,
        activities=[noop_activity, control_activity],
    ) as worker:
        handle = await codec_client.start_workflow(
            LabelPayloadWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()
        events = await _fetch_events(handle)
        run_id = _run_id(handle)
        a_id = _default_marker_id(run_id, "aaa")

        activity_a = _activity_event(events, "activity-a")
        activity_b = _activity_event(events, "activity-b")

        # EG-LABEL-PAYLOAD-21: IDs are not codec-encoded
        _assert_marker_ids(activity_a, _label_marker_id(a_id))
        _assert_marker_ids(activity_b, _label_marker_id("b-id"))

        # EG-LABEL-PAYLOAD-20: label payloads are processed by payload codecs
        assert _label_payload_of(activity_a, a_id)[0] == "binary/wrapped"
        assert _label_payload_of(activity_b, "b-id")[0] == "binary/wrapped"
        decoded_a = (await codec.decode([_raw_label_payload(activity_a, a_id)]))[0]
        decoded_b = (await codec.decode([_raw_label_payload(activity_b, "b-id")]))[0]
        assert PayloadConverter.default.from_payload(decoded_a) == "aaa"
        assert PayloadConverter.default.from_payload(decoded_b) == "bbb"


####################################################################################################
# 3. Explicit Event Group Scopes (`EG-SCOPE`)
####################################################################################################


@workflow.defn
class ScopeBaselineWorkflow:
    @workflow.run
    async def run(self) -> None:
        a = workflow.create_event_group("aaa")
        with a.scope():
            await _activity("activity")
            await workflow.sleep(0.001)
            await workflow.start_child_workflow(
                NoopChildWorkflow.run,
                id=f"{workflow.info().workflow_id}_child",
            )


async def test_commands_in_a_scope_carry_its_marker(
    client: Client, env: WorkflowEnvironment
):
    _require_event_groups_server(env)

    async with new_worker(
        client,
        ScopeBaselineWorkflow,
        NoopChildWorkflow,
        activities=[noop_activity],
    ) as worker:
        handle = await client.start_workflow(
            ScopeBaselineWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()
        events = await _fetch_events(handle)
        a = _label_marker(_default_marker_id(_run_id(handle), "aaa"), "aaa")

        # EG-SCOPE-00: baseline only; per-command coverage lives in EG-COMMANDS
        _assert_markers(_activity_event(events, "activity"), a)
        _assert_markers(_single_event(events, EventType.EVENT_TYPE_TIMER_STARTED), a)
        _assert_markers(
            _single_event(
                events, EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED
            ),
            a,
        )


@workflow.defn
class NestedScopesWorkflow:
    @workflow.run
    async def run(self) -> None:
        a = workflow.create_event_group("aaa")
        b = workflow.create_event_group("bbb")
        with a.scope():
            await _activity("a-before")
            with b.scope():
                await _activity("a-b")
            await _activity("a-after")
        await _activity("outside")


async def test_nesting_scopes_composes(client: Client, env: WorkflowEnvironment):
    _require_event_groups_server(env)

    async with new_worker(
        client, NestedScopesWorkflow, activities=[noop_activity]
    ) as worker:
        handle = await client.start_workflow(
            NestedScopesWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()
        events = await _fetch_events(handle)
        run_id = _run_id(handle)
        a = _label_marker(_default_marker_id(run_id, "aaa"), "aaa")
        b = _label_marker(_default_marker_id(run_id, "bbb"), "bbb")

        assert (
            len(_events_of_type(events, EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED))
            == 4
        )

        # EG-SCOPE-01
        _assert_markers(_activity_event(events, "a-before"), a)
        _assert_markers(_activity_event(events, "a-b"), a, b)
        _assert_markers(_activity_event(events, "a-after"), a)
        _assert_markers(_activity_event(events, "outside"))


@workflow.defn
class ReenteredScopeWorkflow:
    @workflow.run
    async def run(self) -> None:
        a = workflow.create_event_group("aaa")
        with a.scope():
            await _activity("a-before")
            with a.scope():
                await _activity("a-inner")
            await _activity("a-after")
        await _activity("outside")


async def test_reentering_a_group_nests_correctly(
    client: Client, env: WorkflowEnvironment
):
    _require_event_groups_server(env)

    async with new_worker(
        client, ReenteredScopeWorkflow, activities=[noop_activity]
    ) as worker:
        handle = await client.start_workflow(
            ReenteredScopeWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()
        events = await _fetch_events(handle)
        a = _label_marker(_default_marker_id(_run_id(handle), "aaa"), "aaa")

        assert (
            len(_events_of_type(events, EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED))
            == 4
        )

        # EG-SCOPE-02: inner re-entry still serializes the marker once
        _assert_markers(_activity_event(events, "a-before"), a)
        _assert_markers(_activity_event(events, "a-inner"), a)
        _assert_markers(_activity_event(events, "a-after"), a)
        _assert_markers(_activity_event(events, "outside"))


@workflow.defn
class ConcurrentScopesWorkflow:
    @workflow.run
    async def run(self) -> None:
        a = workflow.create_event_group("aaa")
        b = workflow.create_event_group("bbb")
        c = workflow.create_event_group("ccc")
        d = workflow.create_event_group("ddd")
        e = workflow.create_event_group("eee")

        async def left() -> None:
            with b.scope():
                with a.scope():
                    with c.scope():
                        await _activity("b-a-c")
                    await _activity("b-a")
                await _activity("b-after-a")

        async def right() -> None:
            with d.scope():
                with a.scope():
                    with e.scope():
                        await _activity("d-a-e")
                    await _activity("d-a")
                await _activity("d-after-a")

        await asyncio.gather(left(), right())
        await _activity("outside")


async def test_a_group_can_be_scoped_from_two_concurrent_branches(
    client: Client, env: WorkflowEnvironment
):
    _require_event_groups_server(env)

    async with new_worker(
        client, ConcurrentScopesWorkflow, activities=[noop_activity]
    ) as worker:
        handle = await client.start_workflow(
            ConcurrentScopesWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()
        events = await _fetch_events(handle)
        run_id = _run_id(handle)
        a = _label_marker(_default_marker_id(run_id, "aaa"), "aaa")
        b = _label_marker(_default_marker_id(run_id, "bbb"), "bbb")
        c = _label_marker(_default_marker_id(run_id, "ccc"), "ccc")
        d = _label_marker(_default_marker_id(run_id, "ddd"), "ddd")
        e = _label_marker(_default_marker_id(run_id, "eee"), "eee")

        assert (
            len(_events_of_type(events, EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED))
            == 7
        )

        # EG-SCOPE-03: a mutable "currently active groups" stack would cross-contaminate here
        _assert_markers(_activity_event(events, "b-a-c"), b, a, c)
        _assert_markers(_activity_event(events, "b-a"), b, a)
        _assert_markers(_activity_event(events, "b-after-a"), b)
        _assert_markers(_activity_event(events, "d-a-e"), d, a, e)
        _assert_markers(_activity_event(events, "d-a"), d, a)
        _assert_markers(_activity_event(events, "d-after-a"), d)
        _assert_markers(_activity_event(events, "outside"))


@workflow.defn
class DetachedTaskScopeWorkflow:
    @workflow.run
    async def run(self) -> None:
        a = workflow.create_event_group("aaa")
        released = False
        started = False

        async def task() -> None:
            nonlocal started
            await _activity("inside-before")
            started = True
            await workflow.wait_condition(lambda: released)
            await _activity("inside-after")

        with a.scope():
            running = asyncio.create_task(task())
            await workflow.wait_condition(lambda: started)
        released = True
        await running
        await _activity("outside")


async def test_a_task_started_inside_a_scope_keeps_it_after_exit(
    client: Client, env: WorkflowEnvironment
):
    _require_event_groups_server(env)

    async with new_worker(
        client, DetachedTaskScopeWorkflow, activities=[noop_activity]
    ) as worker:
        handle = await client.start_workflow(
            DetachedTaskScopeWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()
        events = await _fetch_events(handle)
        a = _label_marker(_default_marker_id(_run_id(handle), "aaa"), "aaa")

        assert (
            len(_events_of_type(events, EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED))
            == 3
        )

        # EG-SCOPE-04: membership is captured when the task is started
        _assert_markers(_activity_event(events, "inside-before"), a)
        _assert_markers(_activity_event(events, "inside-after"), a)
        _assert_markers(_activity_event(events, "outside"))


@workflow.defn
class OutsiderTaskScopeWorkflow:
    @workflow.run
    async def run(self) -> None:
        a = workflow.create_event_group("aaa")
        release = False

        async def outsider() -> None:
            await workflow.wait_condition(lambda: release)
            await _activity("outside-task")

        running = asyncio.create_task(outsider())
        with a.scope():
            await _activity("in-a")
            release = True
            await running


async def test_a_task_created_outside_a_scope_does_not_inherit_it(
    client: Client, env: WorkflowEnvironment
):
    _require_event_groups_server(env)

    async with new_worker(
        client, OutsiderTaskScopeWorkflow, activities=[noop_activity]
    ) as worker:
        handle = await client.start_workflow(
            OutsiderTaskScopeWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()
        events = await _fetch_events(handle)
        a = _label_marker(_default_marker_id(_run_id(handle), "aaa"), "aaa")

        assert (
            len(_events_of_type(events, EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED))
            == 2
        )

        # EG-SCOPE-05: membership follows the context the code was started in
        _assert_markers(_activity_event(events, "in-a"), a)
        _assert_markers(_activity_event(events, "outside-task"))


@workflow.defn
class ThrowingScopeWorkflow:
    @workflow.run
    async def run(self) -> None:
        a = workflow.create_event_group("aaa")
        b = workflow.create_event_group("bbb")
        with a.scope():
            try:
                with b.scope():
                    await _activity("a-b")
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
            await _activity("a-after")


async def test_a_scope_unwinds_cleanly_when_its_body_throws(
    client: Client, env: WorkflowEnvironment
):
    _require_event_groups_server(env)

    async with new_worker(
        client, ThrowingScopeWorkflow, activities=[noop_activity]
    ) as worker:
        handle = await client.start_workflow(
            ThrowingScopeWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()
        events = await _fetch_events(handle)
        run_id = _run_id(handle)
        a = _label_marker(_default_marker_id(run_id, "aaa"), "aaa")
        b = _label_marker(_default_marker_id(run_id, "bbb"), "bbb")

        assert (
            len(_events_of_type(events, EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED))
            == 2
        )

        # EG-SCOPE-06
        _assert_markers(_activity_event(events, "a-b"), a, b)
        _assert_markers(_activity_event(events, "a-after"), a)


####################################################################################################
# 4. Implicit Event Groups (`EG-IMPLICIT`)
####################################################################################################


@workflow.defn
class StaticSignalHandlerWorkflow:
    def __init__(self) -> None:
        self._done = False

    @workflow.run
    async def run(self) -> None:
        await _activity("from-main-before-signal")
        await workflow.wait_condition(lambda: self._done)
        await _activity("from-main-after-signal")

    @workflow.signal
    async def my_signal(self) -> None:
        await _activity("from-static-signal")
        a = workflow.create_event_group("aaa")
        with a.scope():
            await _activity("from-static-signal-scoped")
        self._done = True


async def test_static_signal_handler_implicit_group(
    client: Client, env: WorkflowEnvironment
):
    _require_event_groups_server(env)

    async with new_worker(
        client, StaticSignalHandlerWorkflow, activities=[noop_activity]
    ) as worker:
        handle = await client.start_workflow(
            StaticSignalHandlerWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.signal(StaticSignalHandlerWorkflow.my_signal)
        await handle.result()
        events = await _fetch_events(handle)
        signal = _event_marker(_signaled_event_ids(events)[0])
        a = _label_marker(_default_marker_id(_run_id(handle), "aaa"), "aaa")

        # EG-IMPLICIT-00
        _assert_markers(_activity_event(events, "from-static-signal"), signal)
        # EG-IMPLICIT-01
        _assert_markers(_activity_event(events, "from-static-signal-scoped"), signal, a)
        # EG-IMPLICIT-30
        _assert_markers(_activity_event(events, "from-main-before-signal"))
        _assert_markers(_activity_event(events, "from-main-after-signal"))


@workflow.defn
class RuntimeSignalHandlerWorkflow:
    def __init__(self) -> None:
        self._done = False

    @workflow.run
    async def run(self) -> None:
        outside = workflow.create_event_group("outside")
        inside = workflow.create_event_group("inside")

        async def on_signal() -> None:
            await _activity("from-runtime-signal")
            with inside.scope():
                await _activity("from-runtime-signal-scoped")
            self._done = True

        with outside.scope():
            workflow.set_signal_handler("mySignal", on_signal)
            await _activity("in-outside")

        await _activity("from-main-before-signal")
        await workflow.wait_condition(lambda: self._done)
        await _activity("from-main-after-signal")


async def test_runtime_signal_handler_implicit_group(
    client: Client, env: WorkflowEnvironment
):
    _require_event_groups_server(env)

    async with new_worker(
        client, RuntimeSignalHandlerWorkflow, activities=[noop_activity]
    ) as worker:
        handle = await client.start_workflow(
            RuntimeSignalHandlerWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.signal("mySignal")
        await handle.result()
        events = await _fetch_events(handle)
        run_id = _run_id(handle)
        signal_ids = _signaled_event_ids(events)
        assert len(signal_ids) == 1
        signal = _event_marker(signal_ids[0])
        outside = _label_marker(_default_marker_id(run_id, "outside"), "outside")
        inside = _label_marker(_default_marker_id(run_id, "inside"), "inside")

        # EG-IMPLICIT-10: handler carries the signaled event, not the registration scope
        _assert_markers(_activity_event(events, "from-runtime-signal"), signal)
        _assert_markers(_activity_event(events, "in-outside"), outside)
        # EG-IMPLICIT-11
        _assert_markers(
            _activity_event(events, "from-runtime-signal-scoped"), signal, inside
        )
        # EG-IMPLICIT-30
        _assert_markers(_activity_event(events, "from-main-before-signal"))
        _assert_markers(_activity_event(events, "from-main-after-signal"))


@workflow.defn
class BufferedSignalWorkflow:
    def __init__(self) -> None:
        self._unblocked = False
        self._handled = False

    @workflow.run
    async def run(self) -> None:
        workflow.set_signal_handler("unblock", self._unblock)
        await workflow.wait_condition(lambda: self._unblocked)

        async def on_signal() -> None:
            await _activity("from-runtime-signal")
            self._handled = True

        workflow.set_signal_handler("mySignal", on_signal)
        await workflow.wait_condition(lambda: self._handled)

    def _unblock(self) -> None:
        self._unblocked = True


async def test_buffered_signal_keeps_its_original_implicit_marker(
    client: Client, env: WorkflowEnvironment
):
    _require_event_groups_server(env)

    async with new_worker(
        client, BufferedSignalWorkflow, activities=[noop_activity]
    ) as worker:
        handle = await client.start_workflow(
            BufferedSignalWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.signal("mySignal")
        await handle.signal("unblock")
        await handle.result()
        events = await _fetch_events(handle)
        signal_ids = _signaled_event_ids(events)
        assert len(signal_ids) == 2
        # mySignal is sent first, so it is the first signaled event
        signal = _event_marker(signal_ids[0])

        # EG-IMPLICIT-12
        _assert_markers(_activity_event(events, "from-runtime-signal"), signal)


@workflow.defn
class CatchAllSignalWorkflow:
    def __init__(self) -> None:
        self._done = False

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self._done)

    @workflow.signal(dynamic=True)
    async def on_any_signal(self, _name: str, _args: Sequence[RawValue]) -> None:
        await _activity("from-catch-all-signal")
        self._done = True


async def test_catch_all_signal_handler_implicit_group(
    client: Client, env: WorkflowEnvironment
):
    _require_event_groups_server(env)

    async with new_worker(
        client, CatchAllSignalWorkflow, activities=[noop_activity]
    ) as worker:
        handle = await client.start_workflow(
            CatchAllSignalWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.signal("non-existent-signal")
        await handle.result()
        events = await _fetch_events(handle)
        signal = _event_marker(_signaled_event_ids(events)[0])

        # EG-IMPLICIT-20
        _assert_markers(_activity_event(events, "from-catch-all-signal"), signal)


@workflow.defn
class StaticUpdateHandlerWorkflow:
    def __init__(self) -> None:
        self._done = False

    @workflow.run
    async def run(self) -> None:
        await _activity("from-main-before-update")
        await workflow.wait_condition(lambda: self._done)
        await _activity("from-main-after-update")

    @workflow.update
    async def my_update(self) -> None:
        await _activity("from-static-update")
        inside = workflow.create_event_group("inside")
        with inside.scope():
            await _activity("from-static-update-scoped")
        self._done = True


async def test_static_update_handler_implicit_group(
    client: Client, env: WorkflowEnvironment
):
    _require_event_groups_server(env)

    update_id = "static-update-1"
    async with new_worker(
        client, StaticUpdateHandlerWorkflow, activities=[noop_activity]
    ) as worker:
        handle = await client.start_workflow(
            StaticUpdateHandlerWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.execute_update(StaticUpdateHandlerWorkflow.my_update, id=update_id)
        await handle.result()
        events = await _fetch_events(handle)
        update = _update_marker(update_id)
        inside = _label_marker(_default_marker_id(_run_id(handle), "inside"), "inside")

        # EG-IMPLICIT-50
        _assert_markers(_activity_event(events, "from-static-update"), update)
        # EG-IMPLICIT-51
        _assert_markers(
            _activity_event(events, "from-static-update-scoped"), update, inside
        )
        # EG-IMPLICIT-80
        _assert_markers(_activity_event(events, "from-main-before-update"))
        _assert_markers(_activity_event(events, "from-main-after-update"))


@workflow.defn
class RuntimeUpdateHandlerWorkflow:
    def __init__(self) -> None:
        self._done = False

    @workflow.run
    async def run(self) -> None:
        outside = workflow.create_event_group("outside")
        inside = workflow.create_event_group("inside")

        async def on_update() -> None:
            await _activity("from-runtime-update")
            with inside.scope():
                await _activity("from-runtime-update-scoped")
            self._done = True

        with outside.scope():
            workflow.set_update_handler("myUpdate", on_update)
            await _activity("in-outside")

        await _activity("from-main-before-update")
        await workflow.wait_condition(lambda: self._done)
        await _activity("from-main-after-update")


async def test_runtime_update_handler_implicit_group(
    client: Client, env: WorkflowEnvironment
):
    _require_event_groups_server(env)

    update_id = "runtime-update-1"
    async with new_worker(
        client, RuntimeUpdateHandlerWorkflow, activities=[noop_activity]
    ) as worker:
        handle = await client.start_workflow(
            RuntimeUpdateHandlerWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Updates that arrive before registration are rejected, not buffered.
        async def handler_registered() -> None:
            events = await _fetch_events(handle)
            assert any(
                e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
                and e.activity_task_scheduled_event_attributes.activity_id
                == "in-outside"
                for e in events
            )

        await assert_eventually(handler_registered)
        await handle.execute_update("myUpdate", id=update_id)
        await handle.result()
        events = await _fetch_events(handle)
        run_id = _run_id(handle)
        update = _update_marker(update_id)
        outside = _label_marker(_default_marker_id(run_id, "outside"), "outside")
        inside = _label_marker(_default_marker_id(run_id, "inside"), "inside")

        # EG-IMPLICIT-60
        _assert_markers(_activity_event(events, "from-runtime-update"), update)
        _assert_markers(_activity_event(events, "in-outside"), outside)
        # EG-IMPLICIT-61
        _assert_markers(
            _activity_event(events, "from-runtime-update-scoped"), update, inside
        )
        # EG-IMPLICIT-80
        _assert_markers(_activity_event(events, "from-main-before-update"))
        _assert_markers(_activity_event(events, "from-main-after-update"))


@workflow.defn
class CatchAllUpdateWorkflow:
    def __init__(self) -> None:
        self._done = False

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self._done)

    @workflow.update(dynamic=True)
    async def on_any_update(self, _name: str, _args: Sequence[RawValue]) -> None:
        await _activity("from-catch-all-update")
        self._done = True


async def test_catch_all_update_handler_implicit_group(
    client: Client, env: WorkflowEnvironment
):
    _require_event_groups_server(env)

    update_id = "catch-all-update-1"
    async with new_worker(
        client, CatchAllUpdateWorkflow, activities=[noop_activity]
    ) as worker:
        handle = await client.start_workflow(
            CatchAllUpdateWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.execute_update("non-existent-update", id=update_id)
        await handle.result()
        events = await _fetch_events(handle)

        # EG-IMPLICIT-70
        _assert_markers(
            _activity_event(events, "from-catch-all-update"), _update_marker(update_id)
        )


####################################################################################################
# 5. Event Group Marker Aggregation (`EG-AGGREGATION`)
####################################################################################################


@workflow.defn
class AggregationWorkflow:
    @workflow.run
    async def run(self) -> None:
        a1 = workflow.create_event_group("aaa")
        a2 = workflow.create_event_group("aaa")
        b1 = workflow.create_event_group("bbb1", id="b-id")
        b2 = workflow.create_event_group("bbb2", id="b-id")

        await _activity("direct-duplicates", [a2, b1, a1, b1, a2, a1])

        with a1.scope():
            with a2.scope():
                with b1.scope():
                    await _activity("nested-scopes")

        with a1.scope():
            with b1.scope():
                await _activity("scope-and-direct-b", [b1])
                await _activity("scope-and-direct-a-b", [b1, a1])

        await _activity("same-instance-twice", [a1, a1])
        await _activity("same-id-direct", [b1, b2])
        with b1.scope():
            await _activity("same-id-scope-and-direct", [b2])


async def test_markers_dedupe_by_id(client: Client, env: WorkflowEnvironment):
    _require_event_groups_server(env)

    async with new_worker(
        client, AggregationWorkflow, activities=[noop_activity]
    ) as worker:
        handle = await client.start_workflow(
            AggregationWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()
        events = await _fetch_events(handle)
        a = _label_marker(_default_marker_id(_run_id(handle), "aaa"), "aaa")
        b = _label_marker("b-id", "bbb1")
        both = (a, b)

        # EG-AGGREGATION-00
        _assert_markers(_activity_event(events, "direct-duplicates"), *both)
        # EG-AGGREGATION-01
        _assert_markers(_activity_event(events, "nested-scopes"), *both)
        # EG-AGGREGATION-02
        _assert_markers(_activity_event(events, "scope-and-direct-b"), *both)
        _assert_markers(_activity_event(events, "scope-and-direct-a-b"), *both)
        # EG-AGGREGATION-03
        _assert_markers(_activity_event(events, "same-instance-twice"), a)
        # EG-AGGREGATION-04: compare IDs only; which label is emitted is unspecified
        _assert_marker_ids(
            _activity_event(events, "same-id-direct"), _label_marker_id("b-id")
        )
        _assert_marker_ids(
            _activity_event(events, "same-id-scope-and-direct"),
            _label_marker_id("b-id"),
        )


####################################################################################################
# 6. Command Type Coverage (`EG-COMMANDS`)
#
# EG-COMMANDS-23 and EG-COMMANDS-24 do not apply: Core-based SDKs have no version/sideEffect API.
# Python continue_as_new always takes options, so there is no short-form counterpart of EG-COMMANDS-40.
# ExternalWorkflowHandle.signal/cancel do not take event_groups; EG-COMMANDS-06/07 assert ambient only.
####################################################################################################


@workflow.defn
class TimerCommandsWorkflow:
    @workflow.run
    async def run(self) -> None:
        direct = workflow.create_event_group("direct")
        scope = workflow.create_event_group("scope")
        with scope.scope():
            await workflow.sleep(0.001, event_groups=[direct])
            try:
                await workflow.wait_condition(
                    lambda: False, timeout=0.001, event_groups=[direct]
                )
            except asyncio.TimeoutError:
                pass
            # A 1ms sleep is the timeout; cancelling the 60s task is the cancel
            # command. Avoid asyncio.wait_for so the timeout timer is a normal
            # sleep and only carries the ambient scope.
            long = asyncio.create_task(workflow.sleep(60, event_groups=[direct]))
            await workflow.sleep(0.001)
            long.cancel()
            await _swallow(long)


async def test_timer_commands_carry_markers(client: Client, env: WorkflowEnvironment):
    _require_event_groups_server(env)

    async with new_worker(client, TimerCommandsWorkflow) as worker:
        handle = await client.start_workflow(
            TimerCommandsWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()
        events = await _fetch_events(handle)
        run_id = _run_id(handle)
        both = (
            _label_marker(_default_marker_id(run_id, "direct"), "direct"),
            _label_marker(_default_marker_id(run_id, "scope"), "scope"),
        )
        ambient = (_label_marker(_default_marker_id(run_id, "scope"), "scope"),)

        timers = _events_of_type(events, EventType.EVENT_TYPE_TIMER_STARTED)
        # sleep, wait_condition timeout, wait_for's 1ms timeout, the cancelled 60s sleep
        assert len(timers) == 4
        cancels = _events_of_type(events, EventType.EVENT_TYPE_TIMER_CANCELED)
        assert len(cancels) == 1

        # EG-COMMANDS-00 and EG-COMMANDS-01 run sequentially, so they are the first two timers
        _assert_markers(timers[0], *both)
        _assert_markers(timers[1], *both)
        # EG-COMMANDS-00-CANCEL: wait_for starts both remaining timers in one task, so select by set
        rest = timers[2:]
        ambient_timers = [t for t in rest if _markers(t) == sorted(ambient)]
        both_timers = [t for t in rest if _markers(t) == sorted(both)]
        assert len(ambient_timers) == 1
        assert len(both_timers) == 1
        _assert_markers(cancels[0], *both)


@workflow.defn
class ActivityCommandsWorkflow:
    @workflow.run
    async def run(self) -> None:
        direct = workflow.create_event_group("direct")
        scope = workflow.create_event_group("scope")
        with scope.scope():
            await workflow.execute_activity(
                noop_activity,
                start_to_close_timeout=_ACT_TIMEOUT,
                schedule_to_start_timeout=timedelta(seconds=10),
                event_groups=[direct],
                activity_id="activity",
            )
            await _swallow(
                asyncio.wait_for(
                    workflow.execute_activity(
                        sleep_activity,
                        start_to_close_timeout=_ACT_TIMEOUT,
                        schedule_to_start_timeout=timedelta(seconds=10),
                        cancellation_type=workflow.ActivityCancellationType.TRY_CANCEL,
                        event_groups=[direct],
                        activity_id="activity-cancelled-sleep-5s",
                    ),
                    timeout=0.001,
                )
            )


async def test_activity_commands_carry_markers(
    client: Client, env: WorkflowEnvironment
):
    _require_event_groups_server(env)

    async with new_worker(
        client,
        ActivityCommandsWorkflow,
        activities=[noop_activity, sleep_activity],
    ) as worker:
        handle = await client.start_workflow(
            ActivityCommandsWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()
        events = await _fetch_events(handle)
        run_id = _run_id(handle)
        both = (
            _label_marker(_default_marker_id(run_id, "direct"), "direct"),
            _label_marker(_default_marker_id(run_id, "scope"), "scope"),
        )

        # EG-COMMANDS-02
        _assert_markers(_activity_event(events, "activity"), *both)
        # EG-COMMANDS-02-CANCEL
        _assert_markers(_activity_event(events, "activity-cancelled-sleep-5s"), *both)
        _assert_markers(
            _single_event(events, EventType.EVENT_TYPE_ACTIVITY_TASK_CANCEL_REQUESTED),
            *both,
        )


@workflow.defn
class LocalActivityCommandsWorkflow:
    @workflow.run
    async def run(self) -> None:
        direct = workflow.create_event_group("direct")
        scope = workflow.create_event_group("scope")
        with scope.scope():
            await workflow.execute_local_activity(
                noop_activity,
                start_to_close_timeout=_ACT_TIMEOUT,
                event_groups=[direct],
                activity_id="local-activity",
            )

            cancel_trigger = workflow.create_event_group("cancel-trigger")
            cancelled_la = workflow.create_event_group("cancelled-la")
            sleeping = asyncio.create_task(
                workflow.execute_local_activity(
                    sleep_activity,
                    start_to_close_timeout=_ACT_TIMEOUT,
                    cancellation_type=workflow.ActivityCancellationType.TRY_CANCEL,
                    event_groups=[direct, cancelled_la],
                    activity_id="cancelled-local-activity-sleep-5s",
                )
            )

            async def trigger() -> None:
                await workflow.execute_local_activity(
                    noop_activity,
                    start_to_close_timeout=_ACT_TIMEOUT,
                    event_groups=[direct, cancel_trigger],
                    activity_id="cancel-trigger",
                )
                sleeping.cancel()

            await asyncio.gather(trigger(), _swallow(sleeping))

            await workflow.execute_local_activity(
                fail_first_activity,
                start_to_close_timeout=_ACT_TIMEOUT,
                local_retry_threshold=timedelta(milliseconds=1),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=1),
                    backoff_coefficient=1,
                    maximum_attempts=2,
                ),
                event_groups=[direct],
                activity_id="backoff-local-activity-fail-first-attempt",
            )


async def test_local_activity_commands_carry_markers(
    client: Client, env: WorkflowEnvironment
):
    _require_event_groups_server(env)

    async with new_worker(
        client,
        LocalActivityCommandsWorkflow,
        activities=[noop_activity, sleep_activity, fail_first_activity],
    ) as worker:
        handle = await client.start_workflow(
            LocalActivityCommandsWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            task_timeout=timedelta(seconds=5),
        )
        await handle.result()
        events = await _fetch_events(handle)
        run_id = _run_id(handle)
        both = (
            _label_marker(_default_marker_id(run_id, "direct"), "direct"),
            _label_marker(_default_marker_id(run_id, "scope"), "scope"),
        )
        cancel_trigger = (
            *both,
            _label_marker(
                _default_marker_id(run_id, "cancel-trigger"), "cancel-trigger"
            ),
        )
        cancelled_la = (
            *both,
            _label_marker(_default_marker_id(run_id, "cancelled-la"), "cancelled-la"),
        )

        local_acts = _markers_named(events, "core_local_activity")
        # plain complete, cancel-trigger complete, cancelled LA, backoff fail, backoff success
        assert len(local_acts) == 5

        # EG-COMMANDS-03
        _assert_markers(local_acts[0], *both)
        # EG-COMMANDS-03-CANCEL
        _assert_markers(local_acts[1], *cancel_trigger)
        _assert_markers(local_acts[2], *cancelled_la)
        # EG-COMMANDS-03-BACKOFF: plan's 10s interval vs 5s WFT timeout is the same Core branch;
        # local_retry_threshold of 1ms reaches it without waiting 10s.
        backoff_timer = _events_of_type(events, EventType.EVENT_TYPE_TIMER_STARTED)
        assert len(backoff_timer) == 1
        _assert_markers(backoff_timer[0], *both)
        _assert_markers(local_acts[3], *both)
        _assert_markers(local_acts[4], *both)


@workflow.defn
class ChildWorkflowCommandsWorkflow:
    @workflow.run
    async def run(self) -> None:
        direct = workflow.create_event_group("direct")
        scope = workflow.create_event_group("scope")
        with scope.scope():
            await workflow.start_child_workflow(
                NoopChildWorkflow.run,
                id=f"{workflow.info().workflow_id}_child",
                event_groups=[direct],
            )
            await _swallow(
                asyncio.wait_for(
                    workflow.execute_child_workflow(
                        SleepChildWorkflow.run,
                        id=f"{workflow.info().workflow_id}_child_cancel",
                        cancellation_type=workflow.ChildWorkflowCancellationType.WAIT_CANCELLATION_REQUESTED,
                        event_groups=[direct],
                    ),
                    timeout=0.001,
                )
            )


async def test_child_workflow_commands_carry_markers(
    client: Client, env: WorkflowEnvironment
):
    _require_event_groups_server(env)

    async with new_worker(
        client,
        ChildWorkflowCommandsWorkflow,
        NoopChildWorkflow,
        SleepChildWorkflow,
    ) as worker:
        handle = await client.start_workflow(
            ChildWorkflowCommandsWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()
        events = await _fetch_events(handle)
        run_id = _run_id(handle)
        both = (
            _label_marker(_default_marker_id(run_id, "direct"), "direct"),
            _label_marker(_default_marker_id(run_id, "scope"), "scope"),
        )

        initiated = _events_of_type(
            events, EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED
        )
        assert len(initiated) == 2
        # EG-COMMANDS-04
        _assert_markers(initiated[0], *both)
        # EG-COMMANDS-04-CANCEL
        _assert_markers(initiated[1], *both)
        _assert_markers(
            _single_event(
                events,
                EventType.EVENT_TYPE_REQUEST_CANCEL_EXTERNAL_WORKFLOW_EXECUTION_INITIATED,
            ),
            *both,
        )


@nexusrpc.handler.service_handler
class EventGroupsNexusService:
    @nexusrpc.handler.sync_operation
    async def nexus_operation(
        self, _ctx: nexusrpc.handler.StartOperationContext, _input: None
    ) -> None:
        return None

    @nexusrpc.handler.sync_operation
    async def nexus_operation_sleep_5s(
        self, _ctx: nexusrpc.handler.StartOperationContext, _input: None
    ) -> None:
        await asyncio.sleep(5)


@workflow.defn
class NexusCommandsWorkflow:
    @workflow.run
    async def run(self, endpoint: str) -> None:
        direct = workflow.create_event_group("direct")
        scope = workflow.create_event_group("scope")
        nexus_client = workflow.create_nexus_client(
            service=EventGroupsNexusService, endpoint=endpoint
        )
        with scope.scope():
            await nexus_client.execute_operation(
                EventGroupsNexusService.nexus_operation,
                None,
                event_groups=[direct],
            )
            # Don't await start/execute to completion: a sync sleeper would finish
            # before cancel, and Core drops a cancel issued in the same WFT as
            # schedule. The 1ms sleep forces a WFT boundary after schedule.
            running = asyncio.create_task(
                nexus_client.execute_operation(
                    EventGroupsNexusService.nexus_operation_sleep_5s,
                    None,
                    cancellation_type=workflow.NexusOperationCancellationType.TRY_CANCEL,
                    event_groups=[direct],
                )
            )
            await workflow.sleep(0.001)
            running.cancel()
            await _swallow(running)


@pytest.mark.requires_local_server
async def test_nexus_operation_commands_carry_markers(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with the time-skipping server")

    task_queue = str(uuid.uuid4())
    endpoint = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint, task_queue)
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[NexusCommandsWorkflow],
        nexus_service_handlers=[EventGroupsNexusService()],
    ):
        handle = await client.start_workflow(
            NexusCommandsWorkflow.run,
            endpoint,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=task_queue,
        )
        await handle.result()
        events = await _fetch_events(handle)
        run_id = _run_id(handle)
        both = (
            _label_marker(_default_marker_id(run_id, "direct"), "direct"),
            _label_marker(_default_marker_id(run_id, "scope"), "scope"),
        )

        scheduled = _events_of_type(
            events, EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED
        )
        assert len(scheduled) == 2
        # EG-COMMANDS-05
        _assert_markers(scheduled[0], *both)
        # EG-COMMANDS-05-CANCEL
        _assert_markers(scheduled[1], *both)
        _assert_markers(
            _single_event(
                events, EventType.EVENT_TYPE_NEXUS_OPERATION_CANCEL_REQUESTED
            ),
            *both,
        )


@workflow.defn
class AmbientOnlyCommandsWorkflow:
    @workflow.run
    async def run(self) -> None:
        scope = workflow.create_event_group("scope")
        with scope.scope():
            child = await workflow.start_child_workflow(
                SleepChildWorkflow.run,
                id=f"{workflow.info().workflow_id}_child",
            )
            await child.signal("noop")
            # External handle cancel of a live child: missing-workflow not-found
            # fails the WFT in a way that is not cleanly catchable here.
            await workflow.get_external_workflow_handle(child.id).cancel()
            workflow.upsert_memo({"some-key": "some-value"})
            workflow.upsert_search_attributes(
                [SearchAttributeKey.for_bool("CustomBoolField").value_set(False)]
            )
            workflow.patched("my-patch-1")
            workflow.deprecate_patch("my-patch-2")


async def test_apis_without_options_carry_ambient_markers(
    client: Client, env: WorkflowEnvironment
):
    _require_event_groups_server(env)
    await ensure_search_attributes_present(
        client, SearchAttributeKey.for_bool("CustomBoolField")
    )

    async with new_worker(
        client, AmbientOnlyCommandsWorkflow, SleepChildWorkflow
    ) as worker:
        handle = await client.start_workflow(
            AmbientOnlyCommandsWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()
        events = await _fetch_events(handle)
        ambient = (
            _label_marker(_default_marker_id(_run_id(handle), "scope"), "scope"),
        )

        # EG-COMMANDS-06, EG-COMMANDS-07: no direct-attach option on the external handle
        _assert_markers(
            _single_event(
                events,
                EventType.EVENT_TYPE_SIGNAL_EXTERNAL_WORKFLOW_EXECUTION_INITIATED,
            ),
            *ambient,
        )
        _assert_markers(
            _single_event(
                events,
                EventType.EVENT_TYPE_REQUEST_CANCEL_EXTERNAL_WORKFLOW_EXECUTION_INITIATED,
            ),
            *ambient,
        )
        # EG-COMMANDS-20
        _assert_markers(
            _single_event(events, EventType.EVENT_TYPE_WORKFLOW_PROPERTIES_MODIFIED),
            *ambient,
        )
        # EG-COMMANDS-22
        patches = _markers_named(events, "core_patch")
        assert len(patches) == 2
        for patch in patches:
            _assert_markers(patch, *ambient)
        # EG-COMMANDS-21 plus the two TemporalChangeVersion upserts beside the patches
        upserts = _events_of_type(
            events, EventType.EVENT_TYPE_UPSERT_WORKFLOW_SEARCH_ATTRIBUTES
        )
        assert len(upserts) == 3
        for upsert in upserts:
            _assert_markers(upsert, *ambient)


@workflow.defn
class ContinueAsNewCommandsWorkflow:
    @workflow.run
    async def run(self, second_run: bool = False) -> None:
        if second_run:
            return
        direct = workflow.create_event_group("direct")
        scope = workflow.create_event_group("scope")
        with scope.scope():
            workflow.continue_as_new(True, event_groups=[direct])


async def test_continue_as_new_carries_markers(
    client: Client, env: WorkflowEnvironment
):
    _require_event_groups_server(env)

    async with new_worker(client, ContinueAsNewCommandsWorkflow) as worker:
        handle = await client.start_workflow(
            ContinueAsNewCommandsWorkflow.run,
            False,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()
        # ContinuedAsNew lives on the first run, which is also the run whose id the markers used
        first_run = client.get_workflow_handle(handle.id, run_id=_run_id(handle))
        events = await _fetch_events(first_run)
        run_id = _run_id(handle)
        both = (
            _label_marker(_default_marker_id(run_id, "direct"), "direct"),
            _label_marker(_default_marker_id(run_id, "scope"), "scope"),
        )

        # EG-COMMANDS-40
        _assert_markers(
            _single_event(
                events, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW
            ),
            *both,
        )


####################################################################################################
# Language-specific
####################################################################################################


@workflow.defn
class EmptyLabelWorkflow:
    @workflow.run
    async def run(self) -> str:
        try:
            workflow.create_event_group("")
        except ValueError as err:
            return str(err)
        return "no error"


async def test_event_group_rejects_empty_label(
    client: Client, env: WorkflowEnvironment
):
    _require_event_groups_server(env)

    async with new_worker(client, EmptyLabelWorkflow) as worker:
        assert "Event group label cannot be empty" == await client.execute_workflow(
            EmptyLabelWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )


def test_create_event_group_requires_workflow_context():
    with pytest.raises(TemporalError):
        workflow.create_event_group("outside-workflow")


####################################################################################################
# History helpers
#
# Markers are rendered as strings so failures print readably and collections can be compared as
# unordered sets (by sorting). ``_render_marker`` includes the label; ``_render_marker_id`` does
# not, for the cases where two groups share an id but not a label and the emitted label is
# unspecified.
####################################################################################################


def _default_marker_id(original_execution_run_id: str, label: str) -> str:
    return hashlib.sha1(f"{original_execution_run_id}{label}".encode()).hexdigest()


def _run_id(handle: WorkflowHandle) -> str:
    assert handle.first_execution_run_id is not None
    return handle.first_execution_run_id


def _events_of_type(
    events: Sequence[HistoryEvent], event_type: EventType.ValueType
) -> list[HistoryEvent]:
    return [e for e in events if e.event_type == event_type]


def _single_event(
    events: Sequence[HistoryEvent], event_type: EventType.ValueType
) -> HistoryEvent:
    matches = _events_of_type(events, event_type)
    assert (
        len(matches) == 1
    ), f"expected 1 {EventType.Name(event_type)}, got {len(matches)}"
    return matches[0]


def _activity_event(events: Sequence[HistoryEvent], activity_id: str) -> HistoryEvent:
    matches = [
        e
        for e in events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        and e.activity_task_scheduled_event_attributes.activity_id == activity_id
    ]
    assert len(matches) == 1, f"expected 1 activity {activity_id!r}, got {len(matches)}"
    return matches[0]


def _markers_named(
    events: Sequence[HistoryEvent], marker_name: str
) -> list[HistoryEvent]:
    return [
        e
        for e in events
        if e.event_type == EventType.EVENT_TYPE_MARKER_RECORDED
        and e.marker_recorded_event_attributes.marker_name == marker_name
    ]


def _signaled_event_ids(events: Sequence[HistoryEvent]) -> list[int]:
    return [
        e.event_id
        for e in events
        if e.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED
    ]


def _render_marker(marker: EventGroupMarker) -> str:
    if marker.HasField("inbound_event"):
        return f"event:{marker.inbound_event.inbound_event_id}"
    if marker.HasField("inbound_update"):
        return f"update:{marker.inbound_update.inbound_update_id}"
    try:
        label = PayloadConverter.default.from_payload(marker.label.label)
        return f"label:{marker.label.id}:{label}"
    except Exception:
        return f"label:{marker.label.id}"


def _render_marker_id(marker: EventGroupMarker) -> str:
    if marker.HasField("inbound_event"):
        return f"event:{marker.inbound_event.inbound_event_id}"
    if marker.HasField("inbound_update"):
        return f"update:{marker.inbound_update.inbound_update_id}"
    return f"label:{marker.label.id}"


def _markers(event: HistoryEvent) -> list[str]:
    return sorted(_render_marker(m) for m in event.event_group_markers)


def _marker_ids(event: HistoryEvent) -> list[str]:
    return sorted(_render_marker_id(m) for m in event.event_group_markers)


def _assert_markers(event: HistoryEvent, *expected: str) -> None:
    actual = [_render_marker(m) for m in event.event_group_markers]
    assert len(actual) == len(
        expected
    ), f"marker count {len(actual)} != {len(expected)}: {actual}"
    assert sorted(actual) == sorted(expected)


def _assert_marker_ids(event: HistoryEvent, *expected: str) -> None:
    actual = [_render_marker_id(m) for m in event.event_group_markers]
    assert len(actual) == len(
        expected
    ), f"marker count {len(actual)} != {len(expected)}: {actual}"
    assert sorted(actual) == sorted(expected)


def _label_marker(group_id: str, label: str) -> str:
    return f"label:{group_id}:{label}"


def _label_marker_id(group_id: str) -> str:
    return f"label:{group_id}"


def _event_marker(event_id: int) -> str:
    return f"event:{event_id}"


def _update_marker(update_id: str) -> str:
    return f"update:{update_id}"


def _label_payload_of(event: HistoryEvent, marker_id: str) -> tuple[str, str]:
    for marker in event.event_group_markers:
        if marker.HasField("label") and marker.label.id == marker_id:
            encoding = marker.label.label.metadata["encoding"].decode()
            return encoding, marker.label.label.data.decode()
    raise AssertionError(f"no label marker {marker_id!r} on event")


def _raw_label_payload(event: HistoryEvent, marker_id: str) -> Payload:
    for marker in event.event_group_markers:
        if marker.HasField("label") and marker.label.id == marker_id:
            return marker.label.label
    raise AssertionError(f"no label marker {marker_id!r} on event")


async def _fetch_events(handle: WorkflowHandle) -> list[HistoryEvent]:
    return list((await handle.fetch_history()).events)


# Module-level so every workflow can issue a uniquely keyed activity without repeating options.
async def _activity(
    activity_id: str,
    event_groups: Sequence[workflow.EventGroup] | None = None,
) -> None:
    await workflow.execute_activity(
        noop_activity,
        start_to_close_timeout=_ACT_TIMEOUT,
        activity_id=activity_id,
        event_groups=event_groups,
    )


async def _swallow(aw: object) -> None:
    try:
        await aw  # type: ignore[misc]
    except (
        asyncio.CancelledError,
        asyncio.TimeoutError,
        ActivityError,
        ChildWorkflowError,
        NexusOperationError,
    ):
        pass


@activity.defn
async def noop_activity() -> None:
    return None


@activity.defn
async def control_activity(value: str) -> str:
    return value


@activity.defn
async def sleep_activity() -> None:
    await asyncio.sleep(5)


@activity.defn
async def fail_first_activity() -> None:
    if activity.info().attempt == 1:
        raise ApplicationError("retry me")


@workflow.defn
class NoopChildWorkflow:
    @workflow.run
    async def run(self) -> None:
        return None


@workflow.defn
class SleepChildWorkflow:
    @workflow.run
    async def run(self) -> None:
        await workflow.sleep(5)
