
from __future__ import annotations
import builtins
from contextlib import contextmanager
from copy import copy

import importlib
import importlib.abc
import importlib.util
import importlib.machinery
import logging
import sys
import types
from typing import Dict, Iterator, Mapping, Optional, Sequence, Set, Union
from typing_extensions import Protocol

from .restrictions import RestrictedWorkflowAccessError, SandboxRestrictions, RestrictedModule, _RestrictionState

logger = logging.getLogger(__name__)

# Set to true to log lots of sandbox details
LOG_TRACE = False
trace_depth = 0
def _trace(message: object, *args: object) -> None:
    if LOG_TRACE:
        global trace_depth
        logger.debug(("  " * trace_depth) + str(message), *args)

class Importer(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(
        self,
        env: _Environment
    ) -> None:
        self.env = env
        self.orig_modules = sys.modules
        self.new_modules: Dict[str, types.ModuleType] = {}
        self.orig_import = builtins.__import__
        print("ORIG_IMPORT", self.orig_import)
        self.modules_checked_for_restrictions: Set[str] = set()

    @contextmanager
    def applied(self) -> Iterator[None]:
        # If our modules are being used, don't do anything
        if sys.modules is self.new_modules:
            yield None
            return
        # Set our modules, then unset on complete
        sys.modules = self.new_modules
        builtins.__import__ = self._import
        try:
            # The first time this is done, we need to manually import sys and builtins
            if "sys" not in self.new_modules:
                print("BB1")
                self.new_modules["sys"] = importlib.__import__("sys")
                print("BB2")
                self.new_modules["builtins"] = importlib.__import__("builtins")
                print("BB3")
                self.new_modules["builtins"].__import__ = self._import # type: ignore
                print("BB4")
                # Reset sys modules again
                sys.modules = self.new_modules
                print("BB5", __builtins__.get('__import__'), __import__) # type: ignore
            yield None
        finally:
            # sys.meta_path.remove(self)
            sys.modules = self.orig_modules
            builtins.__import__ = self.orig_import
            print("DD3", __import__, __builtins__.get("__import__")) # type: ignore

    @contextmanager
    def unapplied(self) -> Iterator[None]:
        # If not is not there, do nothing
        # if self not in sys.meta_path:
        if sys.modules is not self.new_modules:
            yield None
            return
        # Remove then put back in front
        # sys.meta_path.remove(self)
        sys.modules = self.orig_modules
        builtins.__import__ = self.orig_import
        try:
            yield None
        finally:
            # sys.meta_path.insert(0, self)
            sys.modules = self.new_modules
            builtins.__import__ = self._import

    def _import(
        self,
        name: str,
        globals: Optional[Mapping[str, object]] = None,
        locals: Optional[Mapping[str, object]] = None,
        fromlist: Sequence[str] = (),
        level: int = 0,
    ) -> types.ModuleType:
        _trace("Importing %s (fromlist: %s, level: %s)", name, fromlist, level)
        global trace_depth
        trace_depth += 1
        
        # Check module restrictions and passthrough modules
        if name not in sys.modules:
            # Make sure not an entirely invalid module
            self.env.assert_valid_module(name)

            # Check if passthrough
            with self.unapplied():
                passthrough_mod = self.env.maybe_passthrough_module(name)
            if passthrough_mod:
                # Load all parents. Usually Python does this for us, but not on
                # passthrough.
                parent, _, child = name.rpartition(".")
                if parent and parent not in sys.modules:
                    _trace(
                        "Importing parent module %s before passing through %s",
                        parent,
                        name,
                    )
                    self._import(parent, globals, locals)
                    # Set the passthrough on the parent
                    setattr(sys.modules[parent], child, passthrough_mod)
                # Set the passthrough on sys.modules and on the parent
                sys.modules[name] = passthrough_mod
                # Put it on the parent
                if parent:
                    setattr(sys.modules[parent], child, sys.modules[name])

        mod = importlib.__import__(name, globals, locals, fromlist, level)
        # Check for restrictions if necessary and apply
        if mod.__name__ not in self.modules_checked_for_restrictions:
            self.modules_checked_for_restrictions.add(mod.__name__)
            restricted_mod = self.env.maybe_restrict_module(mod)
            if restricted_mod:
                sys.modules[mod.__name__] = restricted_mod
                mod = restricted_mod

        trace_depth -= 1
        return mod

    def find_spec(
        self,
        fullname: str,
        path: Optional[Sequence[Union[bytes, str]]],
        target: Optional[types.ModuleType] = None,
    ) -> Optional[importlib.machinery.ModuleSpec]:
        # Find spec via traditional means
        _trace("Finding spec for module %s at %s", fullname, path)
        # Make sure it's valid
        self.env.assert_valid_module(fullname)
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is not None:
            # Shallow copy the spec and change the loader to ourself
            new_spec = copy(spec)
            # new_spec.loader = self
            setattr(new_spec, "__temporal_orig_spec", spec)
            spec = new_spec
        return spec

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> Optional[types.ModuleType]:
        _trace("Applying custom create for module %s", spec.name)
        global trace_depth
        trace_depth += 1
        orig_spec: importlib.machinery.ModuleSpec = getattr(spec, "__temporal_orig_spec")
        mod = importlib.util.module_from_spec(orig_spec)
        trace_depth -= 1
        return mod

        # # If this is a passthrough module, pass it through. Otherwise, create
        # # it using the spec's original creator. We have to run this without
        # # ourselves on the meta path so passthrough can use existing importer.
        # with self.unapplied():
        #     mod = self.env.maybe_passthrough_module(spec.name)
        # if mod is not None:
        #     # We have to mark it as passthrough so it doesn't execute
        #     setattr(mod, "__temporal_passthrough", True)
        #     # When a module is passed through, all of its parents need to be
        #     # passed through as well if not otherwise loaded
        #     # TODO(cretz): Add restrictions to these too if needed
        # else:
        #     orig_spec: importlib.machinery.ModuleSpec = getattr(spec, "__temporal_orig_spec")
        #     mod = importlib.util.module_from_spec(orig_spec)

        # # Wrap as restricted if needed
        # # restricted_mod = self.env.maybe_restrict_module(mod)
        # # if restricted_mod is not None:
        # #     mod = restricted_mod
        # return mod

    def exec_module(self, module: types.ModuleType) -> None:
        # If it's a passthrough, no exec
        # if getattr(module, "__temporal_passthrough", False):
        #     return
        _trace("Applying custom execute for module %s", module.__name__)
        global trace_depth
        trace_depth += 1
        # mod_builtins = module.__dict__.get("__builtins__")
        # if mod_builtins:
        #     print("IMPORTER", getattr(mod_builtins, "__import__"))
        # print("MY IMPORTER", builtins.__import__)
        module.__dict__["__builtins__"] = builtins
        # print("EXEC", module.__name__, __builtins__.get('__import__'), __import__) # type: ignore
        module.__loader__.exec_module(module) # type: ignore
        trace_depth -= 1
        # # print("SPEC", module.__spec__.name, module.__loader__)
        # # orig_spec: importlib.machinery.ModuleSpec = getattr(module.__spec__, "__temporal_orig_spec")
        # # assert orig_spec.loader
        # with self.unapplied():
        #     # If the loader is ourself, use original spec loader
        #     if module.__loader__ is self:
        #         orig_spec: importlib.machinery.ModuleSpec = getattr(module.__spec__, "__temporal_orig_spec")
        #         orig_spec.loader.exec_module(module) # type: ignore
        #     else:
        #         module.__loader__.exec_module(module) # type: ignore
        #     # orig_spec.loader.exec_module(module)
            
        #     # state: Optional[_RestrictionState] = None
        #     # if type(module) is _RestrictedModule:
        #     #     state = _RestrictionState.from_proxy(module)
        #     #     state.wrap_dict = False
        #     # try:
        #     #     orig_spec.loader.exec_module(module)
        #     # finally:
        #     #     if state:
        #     #         state.wrap_dict = True

class _Environment(Protocol):
    def assert_valid_module(self, name: str) -> None:
        ...

    def maybe_passthrough_module(self, name: str) -> Optional[types.ModuleType]:
        ...

    def maybe_restrict_module(
        self, mod: types.ModuleType
    ) -> Optional[types.ModuleType]:
        ...

class _RestrictedEnvironment:
    def __init__(self, restrictions: SandboxRestrictions) -> None:
        self.restrictions = restrictions

    def assert_valid_module(self, name: str) -> None:
        if self.restrictions.invalid_modules.match_access(*name.split(".")):
            raise RestrictedWorkflowAccessError(name)

    def maybe_passthrough_module(self, name: str) -> Optional[types.ModuleType]:
        if not self.restrictions.passthrough_modules.match_access(*name.split(".")):
            return None
        _trace("Passing module %s through from host", name)
        global trace_depth
        trace_depth += 1
        # Use our import outside of the sandbox
        ret = importlib.import_module(name)
        trace_depth -= 1
        return ret

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
        return RestrictedModule(mod, matcher)