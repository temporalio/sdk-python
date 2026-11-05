import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import temporalio.client
import temporalio.converter
import temporalio.exceptions
import temporalio.worker
import temporalio.worker.workflow_sandbox
import temporalio.workflow
from temporalio._workflow_context import _WorkflowLogicFlag
from tests.helpers import assert_eq_eventually


@temporalio.workflow.defn
class DatetimeArgumentWorkflow:
    @temporalio.workflow.run
    async def run(self, when: datetime) -> str:
        return when.isoformat()


@dataclass
class DatetimeInput:
    when: datetime


@temporalio.workflow.defn
class DatetimeDataclassWorkflow:
    @temporalio.workflow.run
    async def run(self, value: DatetimeInput) -> str:
        return value.when.isoformat()


@temporalio.workflow.defn
class DatetimeListWorkflow:
    @temporalio.workflow.run
    async def run(self, values: list[datetime]) -> str:
        return values[0].isoformat()


@pytest.mark.parametrize("runner", ["sandboxed", "passthrough", "unsandboxed"])
@pytest.mark.parametrize("datetime_decoding_enabled", [False, True])
@pytest.mark.parametrize("shape", ["bare", "list", "dataclass"])
async def test_datetime_arguments_respect_workflow_flag(
    client: temporalio.client.Client,
    runner: str,
    datetime_decoding_enabled: bool,
    shape: str,
):
    workflow_runner: temporalio.worker.WorkflowRunner
    if runner == "unsandboxed":
        workflow_runner = temporalio.worker.UnsandboxedWorkflowRunner()
    else:
        restrictions = temporalio.worker.workflow_sandbox.SandboxRestrictions.default
        if runner == "passthrough":
            restrictions = restrictions.with_passthrough_modules(__name__)
        workflow_runner = temporalio.worker.workflow_sandbox.SandboxedWorkflowRunner(
            restrictions
        )

    workflow_type = {
        "bare": DatetimeArgumentWorkflow,
        "list": DatetimeListWorkflow,
        "dataclass": DatetimeDataclassWorkflow,
    }[shape]
    when = datetime(2021, 1, 2, tzinfo=timezone.utc)
    argument = {"bare": when, "list": [when], "dataclass": DatetimeInput(when)}[shape]
    worker = temporalio.worker.Worker(
        client,
        task_queue=str(uuid.uuid4()),
        workflows=[workflow_type],
        workflow_runner=workflow_runner,
        # Fail decoding errors immediately instead of retrying workflow tasks forever.
        workflow_failure_exception_types=[Exception],
    )
    if not datetime_decoding_enabled:
        worker._set_default_workflow_logic_flag(
            _WorkflowLogicFlag.DECODE_DATETIME_PROXIES, enabled=False
        )
    async with worker:
        handle = await client.start_workflow(
            workflow_type.__name__,
            argument,
            id=str(uuid.uuid4()),
            task_queue=worker.task_queue,
        )
        if runner == "sandboxed" and not datetime_decoding_enabled:
            with pytest.raises(temporalio.client.WorkflowFailureError) as err:
                await handle.result()
            failure = err.value.cause
            while (
                isinstance(failure, temporalio.exceptions.FailureError)
                and failure.cause
            ):
                failure = failure.cause
            assert failure
            assert "Unserializable type during conversion" in str(failure)
        else:
            assert await handle.result() == when.isoformat()

    history = await handle.fetch_history()
    used_flags = {
        flag
        for event in history.events
        if event.HasField("workflow_task_completed_event_attributes")
        for flag in event.workflow_task_completed_event_attributes.sdk_metadata.lang_used_flags
    }
    assert (_WorkflowLogicFlag.DECODE_DATETIME_PROXIES in used_flags) == (
        datetime_decoding_enabled
    )
    await temporalio.worker.Replayer(
        workflows=[workflow_type],
        workflow_runner=workflow_runner,
        workflow_failure_exception_types=[Exception],
    ).replay_workflow(history)


@temporalio.workflow.defn
class DatetimeSignalWorkflow:
    def __init__(self) -> None:
        self.when = ""

    @temporalio.workflow.run
    async def run(self) -> str:
        try:
            await temporalio.workflow.wait_condition(
                lambda: bool(self.when), timeout=timedelta(milliseconds=200)
            )
        except TimeoutError:
            return "dropped"
        await temporalio.workflow.sleep(0.01)
        # An extra command makes processing a formerly dropped signal detectable on replay.
        await temporalio.workflow.sleep(0.01)
        return self.when

    @temporalio.workflow.signal
    def set_when(self, when: datetime) -> None:
        self.when = when.isoformat()


async def test_datetime_signal_records_workflow_flag(
    client: temporalio.client.Client,
) -> None:
    worker = temporalio.worker.Worker(
        client,
        task_queue=str(uuid.uuid4()),
        workflows=[DatetimeSignalWorkflow],
    )
    when = datetime(2021, 1, 2, tzinfo=timezone.utc)
    handle = await client.start_workflow(
        DatetimeSignalWorkflow.run,
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    await handle.signal(DatetimeSignalWorkflow.set_when, when)
    async with worker:
        assert await handle.result() == when.isoformat()

    history = await handle.fetch_history()
    assert any(
        _WorkflowLogicFlag.DECODE_DATETIME_PROXIES
        in event.workflow_task_completed_event_attributes.sdk_metadata.lang_used_flags
        for event in history.events
        if event.HasField("workflow_task_completed_event_attributes")
    )
    await temporalio.worker.Replayer(
        workflows=[DatetimeSignalWorkflow]
    ).replay_workflow(history)


def _load_unflagged_datetime_history() -> temporalio.client.WorkflowHistory:
    return temporalio.client.WorkflowHistory.from_json(
        "datetime-signal-legacy",
        Path(__file__)
        .with_name("test_workflow_flags_datetime_unflagged_history.json")
        .read_text(),
    )


async def test_replay_preserves_dropped_datetime_signal_without_flag() -> None:
    history = _load_unflagged_datetime_history()
    completed_tasks = [
        event.workflow_task_completed_event_attributes
        for event in history.events
        if event.HasField("workflow_task_completed_event_attributes")
    ]
    assert completed_tasks
    assert all(
        _WorkflowLogicFlag.DECODE_DATETIME_PROXIES
        not in completed.sdk_metadata.lang_used_flags
        for completed in completed_tasks
    )
    result = await temporalio.worker.Replayer(
        workflows=[DatetimeSignalWorkflow]
    ).replay_workflow(history, raise_on_replay_failure=False)
    assert result.replay_failure is None
    completion = result.history.events[-1].workflow_execution_completed_event_attributes
    assert temporalio.converter.DefaultPayloadConverter().from_payloads(
        completion.result.payloads
    ) == ["dropped"]


@temporalio.workflow.defn(name="DatetimeSignalWorkflow")
class UntypedDatetimeSignalWorkflow(DatetimeSignalWorkflow):
    @temporalio.workflow.run
    async def run(self) -> str:
        return await super().run()

    @temporalio.workflow.signal
    def set_when(self, when: Any) -> None:
        # Without a datetime hint, the recorded JSON string reaches the handler.
        self.when = when


async def test_replay_rejects_handling_previously_dropped_datetime_signal() -> None:
    history = _load_unflagged_datetime_history()
    original_history = history.to_json()
    result = await temporalio.worker.Replayer(
        workflows=[UntypedDatetimeSignalWorkflow]
    ).replay_workflow(history, raise_on_replay_failure=False)
    assert isinstance(result.replay_failure, temporalio.workflow.NondeterminismError)
    assert history.to_json() == original_history


@temporalio.workflow.defn
class DatetimeMemoWorkflow:
    def __init__(self) -> None:
        self.ready = False
        self.finished = False

    @temporalio.workflow.run
    async def run(self, read_after_signal: bool) -> str:
        self.ready = True
        if read_after_signal:
            await temporalio.workflow.wait_condition(lambda: self.finished)
        return temporalio.workflow.memo_value("when", type_hint=datetime).isoformat()

    @temporalio.workflow.query
    def is_ready(self) -> bool:
        return self.ready

    @temporalio.workflow.signal
    def finish(self) -> None:
        self.finished = True


@pytest.mark.parametrize("read_after_signal", [False, True])
@pytest.mark.parametrize("datetime_decoding_enabled", [False, True])
async def test_workflow_flag_applies_to_typed_memo(
    client: temporalio.client.Client,
    read_after_signal: bool,
    datetime_decoding_enabled: bool,
) -> None:
    worker = temporalio.worker.Worker(
        client,
        task_queue=str(uuid.uuid4()),
        workflows=[DatetimeMemoWorkflow],
        workflow_failure_exception_types=[Exception],
    )
    if not datetime_decoding_enabled:
        worker._set_default_workflow_logic_flag(
            _WorkflowLogicFlag.DECODE_DATETIME_PROXIES, enabled=False
        )
    when = datetime(2021, 1, 2, tzinfo=timezone.utc)
    async with worker:
        handle = await client.start_workflow(
            DatetimeMemoWorkflow.run,
            read_after_signal,
            id=str(uuid.uuid4()),
            task_queue=worker.task_queue,
            memo={"when": when},
        )
        if read_after_signal:

            async def ready() -> bool:
                return await handle.query(DatetimeMemoWorkflow.is_ready)

            await assert_eq_eventually(True, ready)
            await handle.signal(DatetimeMemoWorkflow.finish)
        if datetime_decoding_enabled:
            assert await handle.result() == when.isoformat()
        else:
            with pytest.raises(temporalio.client.WorkflowFailureError):
                await handle.result()
    history = await handle.fetch_history()
    if read_after_signal:
        task_flags = [
            event.workflow_task_completed_event_attributes.sdk_metadata.lang_used_flags
            for event in history.events
            if event.HasField("workflow_task_completed_event_attributes")
        ]
        assert (_WorkflowLogicFlag.DECODE_DATETIME_PROXIES in task_flags[0]) == (
            datetime_decoding_enabled
        )
        assert len(task_flags) >= 2
    await temporalio.worker.Replayer(
        workflows=[DatetimeMemoWorkflow],
        workflow_failure_exception_types=[Exception],
    ).replay_workflow(history)


@temporalio.workflow.defn
class DatetimeQueryWorkflow:
    def __init__(self) -> None:
        self.when = ""
        self.finished = False

    @temporalio.workflow.run
    async def run(self) -> None:
        await temporalio.workflow.wait_condition(lambda: self.finished)

    @temporalio.workflow.query
    def echo(self, when: datetime) -> str:
        return when.isoformat()

    @temporalio.workflow.query
    def state(self) -> str:
        return self.when

    @temporalio.workflow.signal
    def set_when(self, when: datetime) -> None:
        self.when = when.isoformat()

    @temporalio.workflow.signal
    def finish(self) -> None:
        self.finished = True


async def test_new_workflow_records_default_flags_before_first_datetime_query(
    client: temporalio.client.Client,
) -> None:
    worker = temporalio.worker.Worker(
        client,
        task_queue=str(uuid.uuid4()),
        workflows=[DatetimeQueryWorkflow],
    )
    when = datetime(2021, 1, 2, tzinfo=timezone.utc)
    async with worker:
        handle = await client.start_workflow(
            DatetimeQueryWorkflow.run,
            id=str(uuid.uuid4()),
            task_queue=worker.task_queue,
        )
        # This workflow has no inputs, and this query does not decode a datetime.
        assert await handle.query(DatetimeQueryWorkflow.state) == ""
        history = await handle.fetch_history()
        completed_tasks = [
            event.workflow_task_completed_event_attributes
            for event in history.events
            if event.HasField("workflow_task_completed_event_attributes")
        ]
        assert completed_tasks
        assert _WorkflowLogicFlag.DECODE_DATETIME_PROXIES in (
            completed_tasks[0].sdk_metadata.lang_used_flags
        )
        # The first datetime query can rely on the flag recorded by the initial task.
        assert await handle.query(DatetimeQueryWorkflow.echo, when) == when.isoformat()
        await handle.signal(DatetimeQueryWorkflow.set_when, when)

        async def state() -> str:
            return await handle.query(DatetimeQueryWorkflow.state)

        await assert_eq_eventually(when.isoformat(), state)
        assert await handle.query(DatetimeQueryWorkflow.echo, when) == when.isoformat()
        await handle.signal(DatetimeQueryWorkflow.finish)
        await handle.result()
    history = await handle.fetch_history()
    assert any(
        _WorkflowLogicFlag.DECODE_DATETIME_PROXIES
        in event.workflow_task_completed_event_attributes.sdk_metadata.lang_used_flags
        for event in history.events
        if event.HasField("workflow_task_completed_event_attributes")
    )
    await temporalio.worker.Replayer(workflows=[DatetimeQueryWorkflow]).replay_workflow(
        history
    )
