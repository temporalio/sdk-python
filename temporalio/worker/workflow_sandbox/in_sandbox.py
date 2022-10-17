from __future__ import annotations

import builtins
import dataclasses
import pickle
import sys
import types
from contextlib import contextmanager
from typing import Dict, Iterator, Mapping, Optional, Sequence, Set, Type

from typing_extensions import Protocol

import temporalio.bridge.proto.workflow_activation
import temporalio.bridge.proto.workflow_completion
import temporalio.worker.workflow_instance
import temporalio.workflow


class _InSandbox:
    def __init__(self, env: _ExternEnvironment) -> None:
        self.env = env
        self.sandboxed_sys_modules: Dict[str, types.ModuleType] = {}
        self.orig_importer = builtins.__import__
        self.restriction_checked_modules: Set[str] = set()
        self.instance: Optional[
            temporalio.worker.workflow_instance.WorkflowInstance
        ] = None

    def validate(self) -> None:
        with self._sandboxed():
            # Just reload the instance details and runner, but ignore runner and just
            # instantiate the workflow class
            instance_details = self._reloaded_instance_details()
            self._reloaded_runner_class()
            return instance_details.defn.cls()

    async def initialize(self) -> None:
        with self._sandboxed():
            instance_details = self._reloaded_instance_details()
            runner_class = self._reloaded_runner_class()
            runner = runner_class()
            self.instance = await runner.create_instance(instance_details)

    def activate(
        self, act: temporalio.bridge.proto.workflow_activation.WorkflowActivation
    ) -> temporalio.bridge.proto.workflow_completion.WorkflowActivationCompletion:
        assert self.instance
        with self._sandboxed():
            return self.instance.activate(act)

    @contextmanager
    def _sandboxed(self) -> Iterator[None]:
        # Replace sys modules and import, then put back when done
        orig_modules = sys.modules
        orig_import = builtins.__import__
        try:
            sys.modules = self.sandboxed_sys_modules
            builtins.__import__ = self._import
            # We intentionally re-import "sys" again
            self._import("sys")
            # Restrict some builtins
            with self.env.builtins_restricted(builtins):
                yield None
        finally:
            sys.modules = orig_modules
            builtins.__import__ = orig_import

    def _import(
        self,
        name: str,
        globals: Optional[Mapping[str, object]] = None,
        locals: Optional[Mapping[str, object]] = None,
        fromlist: Sequence[str] = (),
        level: int = 0,
    ) -> types.ModuleType:
        new_sys = False

        # If not already in sys mods, check if it's a passthrough and get from there
        if name not in sys.modules:
            new_sys = name == "sys"
            # Make sure entire module isn't invalid
            self.env.assert_valid_module(name)

            # Try to load passthrough module if passthrough
            passthrough_mod = self.env.maybe_passthrough_module(name)
            if passthrough_mod is not None:
                # Internally, Python loads the parents before the children, but
                # we are skipping that when we set the module explicitly here.
                # So we must manually load the parent if there is one.
                parent, _, child = name.rpartition(".")
                if parent and parent not in sys.modules:
                    self.env.trace(
                        "Importing parent module %s before passing through %s",
                        parent,
                        name,
                    )
                    self._import(parent, globals, locals)
                # Just set the module
                self.env.trace("Passing module %s through from host", name)
                sys.modules[name] = passthrough_mod
                # Put it on the parent
                if parent:
                    setattr(sys.modules[parent], child, sys.modules[name])

        # Use original importer. This is essentially a noop if it's in sys.modules
        # already.
        mod = self.orig_importer(name, globals, locals, fromlist, level)

        # Restrict if needed and not done already
        if name not in self.restriction_checked_modules:
            self.restriction_checked_modules.add(name)
            restricted_mod = self.env.maybe_restrict_module(mod)
            if restricted_mod is not None:
                mod = restricted_mod
                if name in sys.modules:
                    sys.modules[name] = mod

        # If this was a new sys import, put the known module dict back
        if new_sys:
            self.sandboxed_sys_modules["sys"] = mod
            setattr(mod, "modules", self.sandboxed_sys_modules)
        return mod

    def _reloaded_instance_details(
        self,
    ) -> temporalio.worker.workflow_instance.WorkflowInstanceDetails:
        # We have to re-import almost everything that comes from the external
        # environment. To do this we use pickle which imports things as needed using
        # __import__. Note, we do not include external functions in pickling, they
        # are set verbatim.
        # TODO(cretz): Pickle too slow and/or are we being too lazy? Would it be
        # better to rework the environment in a more serializable way?
        return dataclasses.replace(
            pickle.loads(self.env.pickled_instance_details),
            extern_functions=self.env.instance_details.extern_functions,
        )

    def _reloaded_runner_class(
        self,
    ) -> Type[temporalio.worker.workflow_instance.WorkflowRunner]:
        return pickle.loads(pickle.dumps(self.env.runner_class))


# Everything in this class comes from outside the sandbox and is already
# pre-loaded/pre-imported.
class _ExternEnvironment(Protocol):
    @property
    def runner_class(self) -> Type[temporalio.worker.workflow_instance.WorkflowRunner]:
        ...

    @property
    def instance_details(
        self,
    ) -> temporalio.worker.workflow_instance.WorkflowInstanceDetails:
        ...

    @property
    def pickled_instance_details(self) -> bytes:
        """Note, this does not include extern_functions."""
        ...

    def trace(self, message: object, *args: object) -> None:
        ...

    def assert_valid_module(self, name: str) -> None:
        ...

    def maybe_passthrough_module(self, name: str) -> Optional[types.ModuleType]:
        ...

    def maybe_restrict_module(
        self, mod: types.ModuleType
    ) -> Optional[types.ModuleType]:
        """May call for module repeatedly, should only restrict on first call."""
        ...

    @contextmanager
    def builtins_restricted(self, bi: types.ModuleType) -> Iterator[None]:
        ...
