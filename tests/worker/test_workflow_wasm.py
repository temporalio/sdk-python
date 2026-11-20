"""End-to-end tests for the opt-in CPython/WASM workflow prototype."""

import asyncio
import os
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
from temporalio import activity
from temporalio.bridge.proto.workflow_activation import WorkflowActivation
from temporalio.bridge.proto.workflow_completion import WorkflowActivationCompletion
from temporalio.client import Client, WorkflowFailureError
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
