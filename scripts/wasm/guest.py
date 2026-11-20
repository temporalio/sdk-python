"""Trusted bootstrap preinitialized by componentize-py."""

from __future__ import annotations

import asyncio
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import io
import linecache
import logging
import os
import random
import sys
import traceback
import zipfile
from functools import cache
from pathlib import Path

from componentize_py_types import Err
from temporal_wasm_bundle_data import ARCHIVE, DATA_CONVERTER, EXTENSIONS, WORKFLOWS
from wit_world.exports import Guest as GuestProtocol
from wit_world.exports.guest import LocalActivities, WorkflowInstance

import temporalio.converter
import temporalio.workflow
from temporalio.bridge.proto.workflow_activation import WorkflowActivation
from temporalio.worker._workflow_info import workflow_info
from temporalio.worker._workflow_instance import (
    WorkflowInstanceDetails,
    _WorkflowInstanceImpl,
    _WorkflowLogicFlag,
)

# This path is mounted by the componentizer only during trusted preinitialization.
_stdlib_bytes = io.BytesIO()
_stdlib_path = Path(os.__file__).parent
with zipfile.ZipFile(_stdlib_bytes, "w", zipfile.ZIP_DEFLATED) as _writer:
    for _path in sorted(_stdlib_path.rglob("*.py")):
        _name = _path.relative_to(_stdlib_path)
        if _name.parts[0] not in {
            "site-packages",
            "test",
            "idlelib",
            "tkinter",
            "ensurepip",
        }:
            _writer.write(_path, _name.as_posix())


class _ArchiveImporter(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, *archives: zipfile.ZipFile) -> None:
        self._sources = {
            name: archive for archive in archives for name in archive.namelist()
        }
        self._packages = {
            "/".join(name.split("/")[:i])
            for name in self._sources.keys() | EXTENSIONS.keys()
            for i in range(1, len(name.split("/")))
        }

    def find_spec(self, fullname, path=None, target=None):
        key = fullname.replace(".", "/")
        for stem, is_package in ((key + "/__init__", True), (key, False)):
            for suffix in importlib.machinery.EXTENSION_SUFFIXES:
                if (origin := EXTENSIONS.get(stem + suffix)) is not None:
                    # The linker resolves this path inside the component without a filesystem.
                    loader = importlib.machinery.ExtensionFileLoader(fullname, origin)
                    return importlib.util.spec_from_file_location(
                        fullname, origin, loader=loader
                    )
            if stem + ".py" in self._sources:
                return importlib.util.spec_from_loader(
                    fullname, self, is_package=is_package
                )
        if key in self._packages:
            spec = importlib.machinery.ModuleSpec(fullname, None, is_package=True)
            spec.submodule_search_locations = [f"<temporal-wasm:{key}>"]
            return spec

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        key = module.__name__.replace(".", "/")
        filename = key + (
            "/__init__.py"
            if module.__spec__.submodule_search_locations is not None
            else ".py"
        )
        origin = f"<temporal-wasm:{filename}>"
        module.__file__ = origin
        exec(
            compile(
                self._sources[filename].read(filename),
                origin,
                "exec",
                dont_inherit=True,
            ),
            module.__dict__,
        )


# Parse the in-memory archive during preinitialization. User modules first run in the guest.
_importer = _ArchiveImporter(
    zipfile.ZipFile(_stdlib_bytes), zipfile.ZipFile(io.BytesIO(ARCHIVE))
)

_data_converter = temporalio.converter.DataConverter.default
if DATA_CONVERTER == "pydantic":
    # Initialize Pydantic's lazy schema machinery while only trusted sources are
    # importable. Workflow discovery otherwise exhausts its fuel on imports.
    from pydantic import create_model

    from temporalio.contrib.pydantic import pydantic_data_converter

    create_model("_TemporalWasmModel", value=(str, ...)).model_validate_json(
        '{"value":"bootstrap"}'
    )
    _data_converter = pydantic_data_converter


@cache
def _definitions() -> dict[str, temporalio.workflow._Definition]:
    # Python's normal path finder would call into the stubbed filesystem for import misses.
    sys.meta_path[:] = [
        importlib.machinery.BuiltinImporter,
        importlib.machinery.FrozenImporter,
        _importer,
    ]
    sys.path[:] = []
    linecache.cache.clear()
    # Tracebacks retain file names and line numbers. Rendering source excerpts can
    # make CPython open files while parsing the expression to highlight.
    linecache.getlines = lambda filename, module_globals=None: []
    random.seed(0)
    logging.disable(logging.CRITICAL)
    definitions: dict[str, temporalio.workflow._Definition] = {}
    for module, class_name in WORKFLOWS:
        workflow_type = importlib.import_module(module)
        for part in class_name.split("."):
            workflow_type = getattr(workflow_type, part)
        definition = temporalio.workflow._Definition.must_from_class(workflow_type)
        name = definition.name or ""
        if name in definitions:
            raise ValueError(f"Duplicate WASM workflow type {name or '<dynamic>'}")
        definitions[name] = definition
    return definitions


class Guest(GuestProtocol):
    def list_workflows(self) -> list[str]:
        try:
            return sorted(_definitions())
        except Exception as exc:
            raise Err(f"{type(exc).__name__}: {exc}") from exc

    def instantiate_workflow(
        self,
        namespace,
        task_queue,
        enabled_logic_flags,
        local_activity_registry,
        disable_eager_activity_execution,
    ) -> WorkflowInstance:
        return WorkflowInstanceImpl(
            namespace,
            task_queue,
            enabled_logic_flags,
            local_activity_registry,
            disable_eager_activity_execution,
        )


class WorkflowInstanceImpl(WorkflowInstance):
    def __init__(
        self,
        namespace,
        task_queue,
        enabled_logic_flags,
        local_activity_registry: LocalActivities | None,
        disable_eager_activity_execution,
    ) -> None:
        self.namespace = namespace
        self.task_queue = task_queue
        self.flags = frozenset(_WorkflowLogicFlag(flag) for flag in enabled_logic_flags)
        self.local_activity_registry = local_activity_registry
        self.disable_eager_activity_execution = disable_eager_activity_execution
        self.instance: _WorkflowInstanceImpl | None = None

    def activate(self, activation: bytes) -> bytes:
        try:
            act = WorkflowActivation.FromString(activation)
            if self.instance is None:
                self.instance = self._initialize(act)
            completion = self.instance.activate(act)
            completion.run_id = act.run_id
            return completion.SerializeToString()
        except Exception as exc:
            raise Err("".join(traceback.format_exception_only(exc)).strip()) from exc

    def _initialize(self, act: WorkflowActivation) -> _WorkflowInstanceImpl:
        init = next(
            job.initialize_workflow
            for job in act.jobs
            if job.HasField("initialize_workflow")
        )
        definitions = _definitions()
        definition = definitions.get(init.workflow_type) or definitions.get("")
        if definition is None:
            raise ValueError(
                f"WASM workflow type {init.workflow_type!r} is not registered"
            )
        details = WorkflowInstanceDetails(
            payload_converter_factory=_data_converter._new_payload_converter,
            failure_converter_class=_data_converter.failure_converter_class,
            interceptor_classes=[],
            defn=definition,
            info=workflow_info(self.namespace, self.task_queue, act, init),
            randomness_seed=init.randomness_seed,
            extern_functions={
                "__temporal_assert_local_activity_valid": self._assert_local_activity_valid,
                "__temporal_get_metric_meter": lambda: (
                    temporalio.common.MetricMeter.noop
                ),
            },
            disable_eager_activity_execution=self.disable_eager_activity_execution,
            worker_level_failure_exception_types=[],
            patch_activation_callback=None,
            last_completion_result=init.last_completion_result,
            last_failure=init.continued_failure
            if init.HasField("continued_failure")
            else None,
            default_workflow_logic_flags=self.flags,
            nexus_serialization_context=temporalio.converter._serialization_context._propagated_nexus_serialization_context(
                init
            ),
        )
        # Instance initialization needs a running-loop registration but never polls a selector.
        asyncio._set_running_loop(asyncio.AbstractEventLoop())
        try:
            return _WorkflowInstanceImpl(details)
        finally:
            asyncio._set_running_loop(None)

    def _assert_local_activity_valid(self, name: str) -> None:
        registry = self.local_activity_registry
        if registry is None or registry.has_dynamic_handler or name in registry.names:
            return
        if not registry.names:
            raise ValueError(
                f"Activity function {name} is not registered on this worker, no available activities."
            )
        raise ValueError(
            f"Activity function {name} is not registered on this worker, "
            f"available activities: {', '.join(registry.names)}"
        )


# The interpreter has already consumed the build-only environment.
os.environ.clear()
