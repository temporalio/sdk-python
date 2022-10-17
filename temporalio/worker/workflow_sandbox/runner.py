from __future__ import annotations

import dataclasses
import functools
import importlib
import logging
import pickle
import sys
import types
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, NoReturn, Optional, Type

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
from .restrictions import (
    RestrictedWorkflowAccessError,
    SandboxRestrictions,
    _RestrictedModule,
)

logger = logging.getLogger(__name__)

# Set to true to log lots of sandbox details
LOG_TRACE = True


def _trace(message: object, *args: object) -> None:
    if LOG_TRACE:
        logger.debug(message, *args)


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
        # Just create with fake info and validate
        _Instance(
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
            self._runner_class,
            self._restrictions,
        ).validate()

    async def create_instance(self, det: WorkflowInstanceDetails) -> WorkflowInstance:
        instance = _Instance(det, self._runner_class, self._restrictions)
        await instance.initialize()
        return instance


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
        self.pickled_instance_details = pickle.dumps(
            dataclasses.replace(self.instance_details, extern_functions={})
        )
        self.runner_class = runner_class
        self.restrictions = restrictions
        # Don't access this outside of exec
        self.in_sandbox: Optional[Any] = None
        # Builtins not properly typed in typeshed
        assert isinstance(__builtins__, dict)
        # Try to get filename off the module
        filename: Optional[str] = None
        try:
            filename = sys.modules[instance_details.defn.cls.__module__].__file__
        except:
            pass
        # Just copy the existing builtins and set ourselves as the env
        self.globals_and_locals = {
            "__builtins__": __builtins__.copy(),
            "__file__": filename or "_unknown_file_.py",
        }

    def trace(self, message: object, *args: object) -> None:
        _trace(message, *args)

    def assert_valid_module(self, name: str) -> None:
        if self.restrictions.passthrough_modules.match_access(*name.split(".")):
            raise RestrictedWorkflowAccessError(name)

    def maybe_passthrough_module(self, name: str) -> Optional[types.ModuleType]:
        if not self.restrictions.passthrough_modules.match_access(*name.split(".")):
            return None
        _trace("Passing module %s through from host", name)
        # Use our import outside of the sandbox
        return importlib.__import__(name)

    def maybe_restrict_module(
        self, mod: types.ModuleType
    ) -> Optional[types.ModuleType]:
        matcher = self.restrictions.invalid_module_members.child_matcher(
            *mod.__name__.split(".")
        )
        if not matcher:
            # No restrictions
            return None
        _trace("Restricting module %s during import", mod.__name__)
        return _RestrictedModule(mod, matcher)

    @contextmanager
    def builtins_restricted(self, bi: types.ModuleType) -> Iterator[None]:
        builtins_matcher = self.restrictions.invalid_module_members.child_matcher(
            "__builtins__"
        )
        if not builtins_matcher:
            yield None
            return
        # Python doesn't allow us to wrap the builtins dictionary with our own
        # __getitem__ type (low-level C assertion is performed to confirm it is
        # a dict), so we instead choose to walk the builtins children and
        # specifically set them as not callable
        def restrict_built_in(name: str, *args, **kwargs) -> NoReturn:
            raise RestrictedWorkflowAccessError(f"__builtins__.{name}")

        to_restore = {}
        try:
            for k in dir(bi):
                if builtins_matcher.match_access(k):
                    to_restore[k] = getattr(bi, k)
                    setattr(bi, k, functools.partial(restrict_built_in, k))
            yield None
        finally:
            for k, v in to_restore.items():
                setattr(bi, k, v)

    def validate(self) -> None:
        self._run_code(
            "from temporalio.worker.workflow_sandbox.in_sandbox import _InSandbox\n"
            "_InSandbox(__temporal_env).validate()",
            __temporal_env=self,
        )

    async def initialize(self) -> None:
        assert not self.in_sandbox
        self._run_code(
            "from temporalio.worker.workflow_sandbox.in_sandbox import _InSandbox\n"
            "import asyncio\n"
            "__temporal_task = asyncio.create_task(_InSandbox(__temporal_env).initialize())",
            __temporal_env=self,
        )
        self.in_sandbox = await self.globals_and_locals.pop("__temporal_task")  # type: ignore

    def activate(
        self, act: temporalio.bridge.proto.workflow_activation.WorkflowActivation
    ) -> temporalio.bridge.proto.workflow_completion.WorkflowActivationCompletion:
        assert self.in_sandbox
        self._run_code(
            "__temporal_completion = __temporal_sandbox.activate(__temporal_activation)",
            __temporal_sandbox=self.in_sandbox,
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
