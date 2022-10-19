from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Type

import temporalio.bridge.proto.workflow_activation
import temporalio.bridge.proto.workflow_completion
import temporalio.converter
import temporalio.worker.workflow_instance
import temporalio.workflow

# Workflow instance has to be relative import
from ...worker.workflow_instance import (
    UnsandboxedWorkflowRunner,
    WorkflowInstance,
    WorkflowInstanceDetails,
    WorkflowRunner,
)
from .restrictions import SandboxRestrictions
from .importer import _RestrictedEnvironment


class SandboxedWorkflowRunner(WorkflowRunner):
    def __init__(
        self,
        # TODO(cretz): Document that this is re-imported and instantiated for
        # _each_ workflow run.
        runner_class: Type[WorkflowRunner] = UnsandboxedWorkflowRunner,
        restrictions: SandboxRestrictions = SandboxRestrictions.default,
    ) -> None:
        super().__init__()
        self._runner_class = runner_class
        self._restrictions = restrictions

    def prepare_workflow(self, defn: temporalio.workflow._Definition) -> None:
        # Just create with fake info which validates
        self.create_instance(
            WorkflowInstanceDetails(
                payload_converter_class=temporalio.converter.default().payload_converter_class,
                interceptor_classes=[],
                defn=defn,
                # Just use fake info during validation
                info=temporalio.workflow.Info(
                    attempt=-1,
                    continued_run_id=None,
                    cron_schedule=None,
                    execution_timeout=None,
                    headers={},
                    namespace="sandbox-validate-namespace",
                    parent=None,
                    raw_memo={},
                    retry_policy=None,
                    run_id="sandbox-validate-run_id",
                    run_timeout=None,
                    search_attributes={},
                    start_time=datetime.fromtimestamp(0, timezone.utc),
                    task_queue="sandbox-validate-task_queue",
                    task_timeout=timedelta(),
                    workflow_id="sandbox-validate-workflow_id",
                    workflow_type="sandbox-validate-workflow_type",
                ),
                randomness_seed=-1,
                extern_functions={},
            ),
        )

    def create_instance(self, det: WorkflowInstanceDetails) -> WorkflowInstance:
        return _Instance(det, self._runner_class, self._restrictions)


# Implements in_sandbox._ExternEnvironment. Some of these calls are called from
# within the sandbox.
class _Instance(WorkflowInstance):
    def __init__(
        self,
        instance_details: WorkflowInstanceDetails,
        runner_class: Type[WorkflowRunner],
        restrictions: SandboxRestrictions,
    ) -> None:
        print("INIT INST", __import__)
        self.restrictions = restrictions
        self.runner_class = runner_class
        self.instance_details = instance_details
        # Builtins not properly typed in typeshed
        assert isinstance(__builtins__, dict)
        # Just copy the existing builtins and set ourselves as the env
        self.globals_and_locals = {
            "__builtins__": __builtins__.copy(),
            "__file__": "workflow_sandbox.py",
        }
        self._create_instance()

    def _create_instance(self) -> None:
        # First create the importer
        print("CREATE", __import__)
        self._run_code(
            "from temporalio.worker.workflow_sandbox.importer import Importer\n"
            "__temporal_importer = Importer(__temporal_importer_environment)\n",
            __temporal_importer_environment=_RestrictedEnvironment(self.restrictions),
        )
        print("WUTUT", self.globals_and_locals.get("__temporal_importer"))
        self.globals_and_locals["__builtins__"]["__import__"] = self.globals_and_locals.get("__temporal_importer")._import # type: ignore
        self._run_code(
            "with __temporal_importer.applied():\n"
            f"  from {self.instance_details.defn.cls.__module__} import {self.instance_details.defn.cls.__name__} as __temporal_workflow_class\n"
            f"  from {self.runner_class.__module__} import {self.runner_class.__name__} as __temporal_runner_class\n"
            "  print('AA4', __builtins__.get('__import__'), __import__)\n"
            "  from temporalio.worker.workflow_sandbox.in_sandbox import InSandbox\n"
            "  __temporal_in_sandbox = InSandbox(__temporal_instance_details, __temporal_runner_class, __temporal_workflow_class)\n",
            __temporal_instance_details=self.instance_details,
            # __temporal_in_sandbox_environment=self,
        )
        print("POST CREATE", __import__)

    # def validate(self) -> None:
    #     self._run_code(
    #         "with __temporal_importer.applied():\n"
    #         "  __temporal_in_sandbox.validate()\n",
    #     )

    # def initialize(self) -> None:
    #     self._run_code(
    #         "with __temporal_importer.applied():\n"
    #         "  import asyncio\n"
    #         "  __temporal_in_sandbox.initialize()\n",
    #     )
    #     self.in_sandbox = await self.globals_and_locals.pop("__temporal_task")  # type: ignore

    def activate(
        self, act: temporalio.bridge.proto.workflow_activation.WorkflowActivation
    ) -> temporalio.bridge.proto.workflow_completion.WorkflowActivationCompletion:
        self._run_code(
            "with __temporal_importer.applied():\n"
            "  __temporal_completion = __temporal_in_sandbox.activate(__temporal_activation)\n",
            __temporal_activation=act,
        )
        return self.globals_and_locals.pop("__temporal_completion")  # type: ignore

    def _run_code(self, code: str, **extra_globals: Any) -> None:
        for k, v in extra_globals.items():
            self.globals_and_locals[k] = v
        try:
            exec(code, self.globals_and_locals, self.globals_and_locals)
        finally:
            for k, v in extra_globals.items():
                del self.globals_and_locals[k]
