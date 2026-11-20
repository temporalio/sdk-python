"""Host-side workflow instance backed by a WASM component."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, cast

import temporalio.bridge.temporal_sdk_bridge
import temporalio.converter
from temporalio.bridge.proto.workflow_activation import WorkflowActivation
from temporalio.bridge.proto.workflow_completion import WorkflowActivationCompletion
from temporalio.workflow import Info

from ._command_aware_visitor import CommandInfo
from ._workflow_instance import WorkflowInstance

_LocalActivityRegistry = tuple[list[str], bool]


class _BridgeInstance(Protocol):
    def activate(self, activation: bytes) -> bytes: ...


class _BridgeBundle(Protocol):
    def __init__(self, component: bytes) -> None: ...

    def data_converter_profile(self) -> str: ...

    def workflow_types(self) -> list[str]: ...

    def instantiate(
        self,
        namespace: str,
        task_queue: str,
        enabled_logic_flags: list[int],
        local_activities: _LocalActivityRegistry | None,
        disable_eager_activity_execution: bool,
    ) -> _BridgeInstance: ...


class _WasmWorkflowBundle:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        bundle_class = getattr(
            temporalio.bridge.temporal_sdk_bridge, "WasmWorkflowBundle", None
        )
        if bundle_class is None:
            raise RuntimeError(
                "WASM workflow bundles require a Python bridge built with the wasm-workflows feature"
            )
        self.component = cast(type[_BridgeBundle], bundle_class)(
            Path(path).read_bytes()
        )
        self.workflow_types = frozenset(self.component.workflow_types())
        self.data_converter_profile = self.component.data_converter_profile()

    def validate_data_converter(
        self, data_converter: temporalio.converter.DataConverter
    ) -> None:
        if self.data_converter_profile == "default":
            expected = temporalio.converter.DataConverter.default
            converter_name = "the default data converter"
        elif self.data_converter_profile == "pydantic":
            from temporalio.contrib.pydantic import pydantic_data_converter

            expected = pydantic_data_converter
            converter_name = "pydantic_data_converter"
        else:
            raise ValueError(
                f"Unknown WASM data converter profile {self.data_converter_profile!r}"
            )
        if (
            type(data_converter) is not type(expected)
            or data_converter.payload_converter_class
            is not expected.payload_converter_class
            or data_converter.failure_converter_class
            is not expected.failure_converter_class
            or data_converter.payload_codec is not None
            or data_converter.external_storage is not None
        ):
            raise ValueError(
                f"WASM workflow bundles with the {self.data_converter_profile} profile "
                f"require {converter_name}"
            )

    def create_instance(
        self,
        info: Info,
        enabled_logic_flags: list[int],
        local_activities: _LocalActivityRegistry | None,
        disable_eager_activity_execution: bool,
    ) -> _WasmWorkflowInstance:
        if (
            info.workflow_type not in self.workflow_types
            and "" not in self.workflow_types
        ):
            raise ValueError(
                f"WASM workflow type {info.workflow_type!r} is not registered"
            )
        return _WasmWorkflowInstance(
            info,
            self.component,
            enabled_logic_flags,
            local_activities,
            disable_eager_activity_execution,
        )


class _WasmWorkflowInstance(WorkflowInstance):
    def __init__(
        self,
        info: Info,
        component: _BridgeBundle,
        enabled_logic_flags: list[int],
        local_activities: _LocalActivityRegistry | None,
        disable_eager_activity_execution: bool,
    ) -> None:
        self._info = info
        self._component = component
        self._enabled_logic_flags = enabled_logic_flags
        self._local_activities = local_activities
        self._disable_eager_activity_execution = disable_eager_activity_execution
        self._instance: _BridgeInstance | None = None
        self._failed = False

    def activate(self, act: WorkflowActivation) -> WorkflowActivationCompletion:
        if self._failed:
            raise RuntimeError("Failed WASM workflow instance is awaiting eviction")
        try:
            if self._instance is None:
                self._instance = self._component.instantiate(
                    self._info.namespace,
                    self._info.task_queue,
                    self._enabled_logic_flags,
                    self._local_activities,
                    self._disable_eager_activity_execution,
                )
            completion = WorkflowActivationCompletion.FromString(
                self._instance.activate(act.SerializeToString())
            )
            if completion.HasField("failed"):
                self._failed = True
                self._instance = None
            return completion
        except Exception:
            self._failed = True
            self._instance = None
            raise

    def get_info(self) -> Info:
        return self._info

    def get_serialization_context(self, command_info: CommandInfo | None) -> None:
        return None

    def get_external_store_context(
        self, command_info: CommandInfo | None
    ) -> temporalio.converter.StorageDriverStoreContext:
        raise RuntimeError("External storage is unavailable for WASM workflow bundles")
