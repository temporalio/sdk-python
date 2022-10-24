"""Runner for workflow sandbox.

.. warning::
    This API for this module is considered unstable and may change in future.
"""

from __future__ import annotations

import threading
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
from .importer import RestrictedEnvironment
from .restrictions import RestrictionContext, SandboxRestrictions


class SandboxedWorkflowRunner(WorkflowRunner):
    """Runner for workflows in a sandbox."""

    def __init__(
        self,
        *,
        restrictions: SandboxRestrictions = SandboxRestrictions.default,
        # TODO(cretz): Document that this is re-imported and instantiated for
        # _each_ workflow run.
        runner_class: Type[WorkflowRunner] = UnsandboxedWorkflowRunner,
    ) -> None:
        """Create the sandboxed workflow runner.

        Args:
            restrictions: Set of restrictions to apply to this sandbox.
            runner_class: The class for underlying runner the sandbox will
                instantiate and  use to run workflows. Note, this class is
                re-imported and instantiated for *each* workflow run.
        """
        super().__init__()
        self._runner_class = runner_class
        self._restrictions = restrictions

    def prepare_workflow(self, defn: temporalio.workflow._Definition) -> None:
        """Implements :py:meth:`WorkflowRunner.prepare_workflow`."""
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
        """Implements :py:meth:`WorkflowRunner.create_instance`."""
        return _Instance(det, self._runner_class, self._restrictions)


# Due to the fact that we are altering the global sys.modules and builtins, we
# have to apply a global lock.
#
# TODO(cretz): Can we get rid of this? It's unfortunate, but I have not
# yet found a way around it. Technically the following code will create
# a new sys:
#
#   new_sys = types.ModuleType("sys")
#   importlib.util.find_spec("sys").loader.exec_module(new_sys)
#   # Shallow copy everything over
#   new_sys.__dict__.update(sys.__dict__.copy())
#   globals()["sys"] = new_sys
#
# But this does not affect other imports. More testing is needed.
_globals_lock = threading.Lock()

# Implements in_sandbox._ExternEnvironment. Some of these calls are called from
# within the sandbox.
class _Instance(WorkflowInstance):
    def __init__(
        self,
        instance_details: WorkflowInstanceDetails,
        runner_class: Type[WorkflowRunner],
        restrictions: SandboxRestrictions,
    ) -> None:
        self.instance_details = instance_details
        self.runner_class = runner_class
        self.restriction_context = RestrictionContext()
        self.importer_environment = RestrictedEnvironment(
            restrictions, self.restriction_context
        )

        # Create the instance
        self.globals_and_locals = {
            "__builtins__": __builtins__.copy(),  # type: ignore
            "__file__": "workflow_sandbox.py",
        }
        self._create_instance()

    def _create_instance(self) -> None:
        _globals_lock.acquire(timeout=5)
        try:
            # First create the importer
            self._run_code(
                "from temporalio.worker.workflow_sandbox.importer import Importer\n"
                "__temporal_importer = Importer(__temporal_importer_environment)\n",
                __temporal_importer_environment=self.importer_environment,
            )

            # Set the importer as the import
            self.globals_and_locals["__builtins__"]["__import__"] = self.globals_and_locals.get("__temporal_importer")._import  # type: ignore

            # Import user code
            self._run_code(
                "with __temporal_importer.applied():\n"
                # Import the workflow code
                f"  from {self.instance_details.defn.cls.__module__} import {self.instance_details.defn.cls.__name__} as __temporal_workflow_class\n"
                f"  from {self.runner_class.__module__} import {self.runner_class.__name__} as __temporal_runner_class\n"
            )

            # Set context as in runtime
            self.restriction_context.is_runtime = True

            # Create the sandbox instance
            self._run_code(
                "with __temporal_importer.applied():\n"
                "  from temporalio.worker.workflow_sandbox.in_sandbox import InSandbox\n"
                "  __temporal_in_sandbox = InSandbox(__temporal_instance_details, __temporal_runner_class, __temporal_workflow_class)\n",
                __temporal_instance_details=self.instance_details,
            )
        finally:
            self.restriction_context.is_runtime = False
            _globals_lock.release()

    def activate(
        self, act: temporalio.bridge.proto.workflow_activation.WorkflowActivation
    ) -> temporalio.bridge.proto.workflow_completion.WorkflowActivationCompletion:
        _globals_lock.acquire(timeout=5)
        self.restriction_context.is_runtime = True
        try:
            self._run_code(
                "with __temporal_importer.applied():\n"
                "  __temporal_completion = __temporal_in_sandbox.activate(__temporal_activation)\n",
                __temporal_activation=act,
            )
            return self.globals_and_locals.pop("__temporal_completion")  # type: ignore
        finally:
            self.restriction_context.is_runtime = False
            _globals_lock.release()

    def _run_code(self, code: str, **extra_globals: Any) -> None:
        for k, v in extra_globals.items():
            self.globals_and_locals[k] = v
        try:
            exec(code, self.globals_and_locals, self.globals_and_locals)
        finally:
            for k, v in extra_globals.items():
                del self.globals_and_locals[k]
