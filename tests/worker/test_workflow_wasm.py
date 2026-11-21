"""End-to-end tests for the opt-in CPython/WASM workflow prototype."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol, cast

import pytest
import temporalio.bridge.temporal_sdk_bridge as temporal_sdk_bridge
from nexusrpc.handler import StartOperationContext, service_handler, sync_operation

import temporalio.worker._workflow as workflow_worker
import temporalio.workflow as workflow
from scripts.build_workflow_wasm import (
    _addressable_modules,
    _discover_addressable_functions,
)
from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.bridge.proto.workflow_activation import WorkflowActivation
from temporalio.bridge.proto.workflow_completion import WorkflowActivationCompletion
from temporalio.client import (
    Client,
    WorkflowFailureError,
    WorkflowHandle,
    WorkflowHistory,
)
from temporalio.common import RawValue
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError, CancelledError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from temporalio.worker._workflow_wasm import _BridgeBundle
from tests.helpers.nexus import make_nexus_endpoint_name
from tests.worker.wasm_fixtures.native import NativeWorkflow

pytestmark = pytest.mark.timeout(240)
_GUEST_SOURCE_DIR = Path(__file__).with_name("wasm_fixtures")


class _BuildBundle(Protocol):
    def __call__(
        self, source: Path, workflows: list[str], output: Path, *options: str
    ) -> None: ...


@pytest.fixture(scope="module")
def wasm_builder() -> _BuildBundle:
    python = os.environ.get("TEMPORAL_WASM_COMPONENTIZER_PYTHON")
    dependency_path = os.environ.get("TEMPORAL_WASM_DEPENDENCY_PATH")
    if (
        not python
        or not dependency_path
        or not hasattr(temporal_sdk_bridge, "WasmWorkflowBundle")
    ):
        pytest.skip("requires componentize-py, guest dependencies, and the WASM bridge")

    def build(source: Path, workflows: list[str], output: Path, *options: str) -> None:
        selection = [part for name in workflows for part in ("--workflow", name)]
        subprocess.run(
            [
                python,
                "scripts/build_workflow_wasm.py",
                "--source",
                str(source),
                "--dependency-path",
                dependency_path,
                *selection,
                "--output",
                str(output),
                *options,
            ],
            check=True,
            timeout=120,
        )

    return build


@pytest.fixture(scope="module")
def wasm_bundle(
    tmp_path_factory: pytest.TempPathFactory, wasm_builder: _BuildBundle
) -> Path:
    temp = tmp_path_factory.mktemp("workflow-wasm")
    output = temp / "workflows.wasm"
    wasm_builder(
        _GUEST_SOURCE_DIR,
        ["guest.workflows:WasmRoundTrip", "guest.workflows:WasmProbe"],
        output,
    )
    return output


@pytest.fixture(scope="module")
def wasm_pydantic_dependencies() -> Path:
    dependency_path = os.environ.get("TEMPORAL_WASM_PYDANTIC_DEPENDENCY_PATH")
    if not dependency_path:
        pytest.skip(
            "requires staged Pydantic guest dependencies and the WASM extension"
        )
    return Path(dependency_path)


@pytest.fixture(scope="module")
def wasm_pydantic_bundle(
    tmp_path_factory: pytest.TempPathFactory,
    wasm_builder: _BuildBundle,
    wasm_pydantic_dependencies: Path,
) -> Path:
    pytest.importorskip("pydantic", minversion="2.11")
    output = tmp_path_factory.mktemp("workflow-wasm-pydantic") / "workflows.wasm"
    wasm_builder(
        _GUEST_SOURCE_DIR,
        ["guest.pydantic_workflow:WasmPydantic"],
        output,
        "--dependency-path",
        str(wasm_pydantic_dependencies),
        "--data-converter",
        "pydantic",
    )
    return output


def test_wasm_import_time_capabilities(
    tmp_path: Path, wasm_builder: _BuildBundle
) -> None:
    output = tmp_path / "blocked.wasm"
    wasm_builder(_GUEST_SOURCE_DIR, ["guest.import_clock:ImportClock"], output)
    with pytest.raises(RuntimeError, match="discovering WASM workflows"):
        getattr(temporal_sdk_bridge, "WasmWorkflowBundle")(output.read_bytes())


def test_wasm_imports_native_extension_from_dependency_path(
    tmp_path: Path, wasm_builder: _BuildBundle
) -> None:
    dependency_path = os.environ.get("TEMPORAL_WASM_NATIVE_DEPENDENCY_PATH")
    if not dependency_path:
        pytest.skip("requires the native extension fixture compiled for WASI")
    output = tmp_path / "native-extension.wasm"
    wasm_builder(
        _GUEST_SOURCE_DIR,
        ["guest.native_extension:WasmNativeExtension"],
        output,
        "--dependency-path",
        dependency_path,
    )

    bundle = cast(
        type[_BridgeBundle], getattr(temporal_sdk_bridge, "WasmWorkflowBundle")
    )(output.read_bytes())
    assert bundle.data_converter_profile() == "default"
    completion = _activate_probe(bundle, False, "WasmNativeExtension")
    result = completion.successful.commands[0].complete_workflow_execution.result
    assert DataConverter.default.payload_converter.from_payloads([result], [int]) == [
        42
    ]
    with pytest.raises(
        RuntimeError, match=r"(?s)activating WASM workflow.*clock_gettime"
    ):
        _activate_probe(bundle, True, "WasmNativeExtension")


@pytest.fixture(scope="module")
def wasm_component(wasm_bundle: Path) -> _BridgeBundle:
    return cast(
        type[_BridgeBundle], getattr(temporal_sdk_bridge, "WasmWorkflowBundle")
    )(wasm_bundle.read_bytes())


def _activate_probe(
    wasm_component: _BridgeBundle, probe: object, workflow_type: str = "WasmProbe"
) -> WorkflowActivationCompletion:
    instance = wasm_component.instantiate("default", "queue", [], ([], False), False)
    act = WorkflowActivation(run_id=str(uuid.uuid4()))
    init = act.jobs.add().initialize_workflow
    init.workflow_type = workflow_type
    init.workflow_id = "probe"
    init.randomness_seed = 17
    init.arguments.extend(DataConverter.default.payload_converter.to_payloads([probe]))
    return WorkflowActivationCompletion.FromString(
        instance.activate(act.SerializeToString())
    )


def test_wasm_limits_and_capabilities(wasm_component: _BridgeBundle) -> None:
    for probe in ["clock", "entropy", "file", "fuel", "memory"]:
        with pytest.raises(RuntimeError, match="activating WASM workflow"):
            _activate_probe(wasm_component, probe)
    with pytest.raises(RuntimeError, match="decode|parsing"):
        _activate_probe(wasm_component, "invalid-completion")
    completion = _activate_probe(wasm_component, "application-failure")
    failure = completion.successful.commands[0].fail_workflow_execution.failure
    assert failure.message == "failed in guest"

    completion = _activate_probe(wasm_component, "local-activity")
    result = completion.successful.commands[0].complete_workflow_execution.result
    assert DataConverter.default.payload_converter.from_payloads([result]) == [
        "Activity function missing is not registered on this worker, no available activities."
    ]


def test_wasm_pydantic_validator_cannot_access_clock(
    wasm_pydantic_bundle: Path,
) -> None:
    component = cast(
        type[_BridgeBundle], getattr(temporal_sdk_bridge, "WasmWorkflowBundle")
    )(wasm_pydantic_bundle.read_bytes())
    instance = component.instantiate("default", "queue", [], None, False)
    act = WorkflowActivation(run_id=str(uuid.uuid4()))
    init = act.jobs.add().initialize_workflow
    init.workflow_type = "WasmPydantic"
    init.workflow_id = "probe"
    init.arguments.extend(
        DataConverter.default.payload_converter.to_payloads(
            [{"displayName": "use-clock"}]
        )
    )
    with pytest.raises(RuntimeError, match="activating WASM workflow"):
        instance.activate(act.SerializeToString())


@pytest.mark.requires_local_server
async def test_wasm_pydantic_round_trip_and_replay(
    client: Client, wasm_pydantic_bundle: Path
) -> None:
    from temporalio.contrib.pydantic import pydantic_data_converter
    from tests.worker.wasm_fixtures.guest.pydantic_workflow import (
        WasmModel,
        WasmPydantic,
    )

    queue = str(uuid.uuid4())
    config = client.config()
    # The shared test environment adds a native workflow interceptor.
    config["interceptors"] = []
    with pytest.raises(
        ValueError, match="pydantic profile require pydantic_data_converter"
    ):
        Worker(
            Client(**config),
            task_queue=queue,
            wasm_workflow_bundle=wasm_pydantic_bundle,
        )

    config["data_converter"] = pydantic_data_converter
    client = Client(**config)
    worker = Worker(client, task_queue=queue, wasm_workflow_bundle=wasm_pydantic_bundle)
    async with asyncio.timeout(45), worker:
        handle = await client.start_workflow(
            WasmPydantic.run,
            WasmModel(displayName=" Temporal "),
            id=str(uuid.uuid4()),
            task_queue=queue,
        )
        assert await handle.result() == WasmModel(displayName="temporal wasm")
        history = await handle.fetch_history()

    completed = history.events[-1].workflow_execution_completed_event_attributes
    assert completed.result.payloads[0].data == b'{"displayName":"TEMPORAL WASM"}'
    await Replayer(
        wasm_workflow_bundle=wasm_pydantic_bundle,
        namespace=client.namespace,
        data_converter=pydantic_data_converter,
    ).replay_workflow(history)


@workflow.defn(dynamic=True)
class DynamicWorkflow:
    @workflow.run
    async def run(self, _args: Sequence[RawValue]) -> None:
        pass


async def test_mixed_workflow_registration_conflicts(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        workflow_worker,
        "_WasmWorkflowBundle",
        lambda _: SimpleNamespace(
            workflow_types=frozenset({"NativeWorkflow", ""}),
            validate_data_converter=lambda _: None,
        ),
    )

    with pytest.raises(ValueError, match="More than one workflow named NativeWorkflow"):
        Worker(
            client,
            task_queue="duplicate-name",
            workflows=[NativeWorkflow],
            wasm_workflow_bundle="workflows.wasm",
        )

    with pytest.raises(TypeError, match="More than one dynamic workflow"):
        Worker(
            client,
            task_queue="duplicate-dynamic",
            workflows=[DynamicWorkflow],
            wasm_workflow_bundle="workflows.wasm",
        )


@pytest.mark.requires_local_server
async def test_wasm_workflow_commands_and_replay(
    client: Client, env: WorkflowEnvironment, wasm_bundle: Path, tmp_path: Path
) -> None:
    queue = str(uuid.uuid4())
    endpoint = make_nexus_endpoint_name(queue)
    await env.create_nexus_endpoint(endpoint, queue)
    calls: list[str] = []

    @activity.defn(name="wasm_local")
    async def local(value: str) -> str:
        calls.append("local")
        if activity.info().attempt == 1:
            raise ApplicationError("retry on the next local activity attempt")
        return f"{value}-local"

    @activity.defn(name="wasm_remote")
    async def remote(value: str) -> str:
        calls.append("remote")
        return f"{value}-remote"

    @service_handler(name="WasmNexus")
    class Nexus:
        @sync_operation
        async def echo(self, _ctx: StartOperationContext, input: str) -> str:
            calls.append("nexus")
            return f"{input}-nexus"

    worker = Worker(
        client,
        task_queue=queue,
        workflows=[NativeWorkflow],
        wasm_workflow_bundle=wasm_bundle,
        activities=[local, remote],
        nexus_service_handlers=[Nexus()],
        max_cached_workflows=0,
    )
    async with asyncio.timeout(45), worker:
        native = await client.start_workflow(
            NativeWorkflow.run, "value", id=str(uuid.uuid4()), task_queue=queue
        )
        assert await native.result() == "native:value"
        native_history = await native.fetch_history()

        handle = await client.start_workflow(
            "WasmRoundTrip", endpoint, id=str(uuid.uuid4()), task_queue=queue
        )
        assert await handle.query("state", result_type=int) == 1
        await handle.signal("finish")
        assert await handle.result() == "guest-local-remote-nexus!:1"
        history = await handle.fetch_history()

        second = await client.start_workflow(
            "WasmRoundTrip", endpoint, id=str(uuid.uuid4()), task_queue=queue
        )
        await second.cancel()
        with pytest.raises(WorkflowFailureError) as cancelled:
            await second.result()
        assert isinstance(cancelled.value.__cause__, CancelledError)

    previous_calls = calls.copy()
    history_paths = []
    for recorded in (history, native_history):
        history_path = tmp_path / f"{recorded.workflow_id}.json"
        history_path.write_text(recorded.to_json())
        history_paths.append(str(history_path))
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.worker.wasm_fixtures.replay",
        str(wasm_bundle),
        client.namespace,
        *history_paths,
    )
    assert await asyncio.wait_for(process.wait(), 120) == 0
    assert calls == previous_calls
    assert previous_calls[:4] == ["local", "local", "remote", "nexus"]


def _addressable_bindings(history: WorkflowHistory) -> list[dict[str, bytes]]:
    return [
        dict(bindings)
        for event in history.events
        if (
            bindings
            := event.workflow_task_completed_event_attributes.sdk_metadata.wasm_function_bindings
        )
    ]


def test_addressable_discovery_uses_selected_classes_and_import_bindings(
    tmp_path: Path,
) -> None:
    package = tmp_path / "demo"
    package.mkdir()
    for name in ("steps", "shadowed", "unselected"):
        (package / f"{name}.py").write_text(
            "async def score(value: int) -> int:\n    return value\n"
            "def other() -> str:\n    return 'still in the bundle'\n"
        )
    (package / "workflows.py").write_text(
        """
from .steps import score as call_score
from .shadowed import score as shadowed
from .unselected import score as unselected

class Selected:
    async def run(self, shadowed):
        return await call_score(1) + await shadowed(2)

class Unselected:
    async def run(self):
        return await unselected(3)
"""
    )

    functions = _discover_addressable_functions(
        tmp_path, [("demo.workflows", "Selected")]
    )
    assert list(functions) == [("demo/steps.py", "score")]
    definition = functions["demo/steps.py", "score"]
    assert definition.slot == "demo.steps:score"
    assert b"def other()" in _addressable_modules(functions)["demo/steps.py"]


@pytest.mark.requires_local_server
@pytest.mark.timeout(360)
async def test_wasm_content_addressed_rollouts(
    client: Client, tmp_path: Path, wasm_builder: _BuildBundle
) -> None:
    fixture = _GUEST_SOURCE_DIR / "addressable"
    source = tmp_path / "source"
    source.mkdir()
    shutil.copyfile(fixture / "workflows.py", source / "workflows.py")
    shutil.copyfile(fixture / "v1" / "steps.py", source / "steps.py")
    bundle = tmp_path / "workflow.wasm"
    store = tmp_path / "functions"
    workflows = ["workflows:AddressableWorkflow", "workflows:AddressableBatch"]
    wasm_builder(source, workflows, bundle, "--addressable")
    slot = "steps:score"

    def publish(version: int) -> dict[str, bytes]:
        shutil.copyfile(fixture / f"v{version}" / "steps.py", source / "steps.py")
        subprocess.run(
            [
                sys.executable,
                "scripts/build_workflow_wasm.py",
                "--source",
                str(source),
                "--publish-addressables",
                str(store),
                *[part for name in workflows for part in ("--workflow", name)],
            ],
            check=True,
            timeout=10,
        )
        return {
            name: bytes.fromhex(digest)
            for name, digest in json.loads(
                (store / "bindings.json").read_text()
            ).items()
        }

    config = client.config()
    config["interceptors"] = []
    client = Client(**config)
    queue = str(uuid.uuid4())
    converter = DataConverter.default.payload_converter
    activity_calls: list[int] = []

    @activity.defn(name="addressable_echo")
    async def echo(value: int) -> int:
        activity_calls.append(value)
        return value

    def worker() -> Worker:
        return Worker(
            client,
            task_queue=queue,
            wasm_workflow_bundle=bundle,
            wasm_addressable_store=store,
            activities=[echo],
            max_cached_workflows=0,
        )

    async def wait_for_memo(
        handle: WorkflowHandle[None, list[tuple[int, list[str]]]], count: int
    ) -> None:
        async for event in handle.fetch_history_events(wait_new_event=True):
            fields = (
                event.workflow_properties_modified_event_attributes.upserted_memo.fields
            )
            if (
                "completed_calls" in fields
                and converter.from_payload(fields["completed_calls"]) == count
            ):
                return
        pytest.fail(f"Workflow closed before recording {count} completed calls")

    async with asyncio.timeout(140):
        first = publish(1)
        async with worker():
            handle = await client.start_workflow(
                "AddressableWorkflow",
                id=str(uuid.uuid4()),
                task_queue=queue,
                result_type=list[tuple[int, list[str]]],
            )
            assert await handle.query("preview", result_type=list[str]) == []
            await handle.signal("advance", 1)
            async for event in handle.fetch_history_events(wait_new_event=True):
                if event.event_type == EventType.EVENT_TYPE_TIMER_STARTED:
                    break
            else:
                pytest.fail("Workflow closed before scheduling the first timer")
            second = publish(2)

        assert all(
            event.event_type != EventType.EVENT_TYPE_WORKFLOW_PROPERTIES_MODIFIED
            for event in (await handle.fetch_history()).events
        )
        async with worker():
            await wait_for_memo(handle, 1)
            await handle.signal("advance", 2)
            await wait_for_memo(handle, 2)
            third = publish(3)

        async with worker():
            await handle.signal("advance", 3)
            assert await handle.result() == [
                (11, ["v1", "1/y/z!?"]),
                (22, ["v2", "2/y/z!?"]),
                (33, ["v3", "3/y/z!?"]),
            ]
            history = await handle.fetch_history()
            batch = await client.start_workflow(
                "AddressableBatch", id=str(uuid.uuid4()), task_queue=queue
            )
            assert await batch.result() == 1000
            batch_history = await batch.fetch_history()

    versioned_slots = {slot, "steps:remember", "steps:remembered"}
    assert (
        first.keys()
        == second.keys()
        == third.keys()
        == (versioned_slots | {"workflows:expected_labels"})
    )
    assert all(
        len({first[name], second[name], third[name]}) == 3 for name in versioned_slots
    )
    assert _addressable_bindings(history) == [first, second, third]
    assert _addressable_bindings(batch_history) == [{slot: third[slot]}]
    assert [
        event.event_type
        for event in history.events
        if event.event_type
        in (
            EventType.EVENT_TYPE_TIMER_STARTED,
            EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED,
            EventType.EVENT_TYPE_MARKER_RECORDED,
        )
    ] == [
        EventType.EVENT_TYPE_TIMER_STARTED,
        EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED,
        EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED,
        EventType.EVENT_TYPE_TIMER_STARTED,
    ]

    history_path = tmp_path / f"{history.workflow_id}.json"
    history_path.write_text(history.to_json())
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.worker.wasm_fixtures.replay_addressable",
        str(bundle),
        str(store),
        str(history_path),
        client.namespace,
    )
    try:
        assert await asyncio.wait_for(process.wait(), 90) == 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert activity_calls == [22, 33]

    def replayer() -> Replayer:
        return Replayer(
            namespace=client.namespace,
            wasm_workflow_bundle=bundle,
            wasm_addressable_store=store,
        )

    forced = WorkflowHistory.from_json(history.workflow_id, history_path.read_text())
    for event in forced.events:
        bindings = event.workflow_task_completed_event_attributes.sdk_metadata.wasm_function_bindings
        if bindings.get(slot) == first[slot]:
            bindings[slot] = third[slot]
            break
    with pytest.raises(workflow.NondeterminismError, match="TimerStarted"):
        await replayer().replay_workflow(forced)

    historical_digest = first["steps:remembered"].hex()
    capsule = store / f"{historical_digest}.wasm"
    original = capsule.read_bytes()
    try:
        capsule.unlink()
        with pytest.raises(
            RuntimeError, match=f"fetching WASM body {historical_digest}"
        ):
            await replayer().replay_workflow(history)
        capsule.write_bytes(original + b"corrupt")
        with pytest.raises(
            RuntimeError, match=f"digest mismatch for WASM body {historical_digest}"
        ):
            await replayer().replay_workflow(history)
    finally:
        capsule.write_bytes(original)
