from __future__ import annotations

import builtins
import importlib
import logging
import sys
import types
from contextlib import contextmanager
from typing import Dict, Iterator, Mapping, Optional, Sequence, Set

from typing_extensions import Protocol

from .restrictions import (
    RestrictedModule,
    RestrictedWorkflowAccessError,
    SandboxRestrictions,
)

logger = logging.getLogger(__name__)

# Set to true to log lots of sandbox details
LOG_TRACE = False
_trace_depth = 0


def _trace(message: object, *args: object) -> None:
    if LOG_TRACE:
        global _trace_depth
        logger.debug(("  " * _trace_depth) + str(message), *args)


class Importer:
    def __init__(self, env: _Environment) -> None:
        self.env = env
        self.orig_modules = sys.modules
        self.new_modules: Dict[str, types.ModuleType] = {}
        self.orig_import = builtins.__import__
        self.modules_checked_for_restrictions: Set[str] = set()
        self.import_func = self._import if not LOG_TRACE else self._traced_import

    # TODO(cretz): Document that this has a global lock
    @contextmanager
    def applied(self) -> Iterator[None]:
        # Error if already applied
        if sys.modules is self.new_modules:
            raise RuntimeError("Sandbox importer already applied")

        # Set our modules, then unset on complete
        sys.modules = self.new_modules
        builtins.__import__ = self.import_func
        try:
            # The first time this is done, we need to manually import a few
            # implied modules
            if "sys" not in self.new_modules:
                # Refresh sys
                self.new_modules["sys"] = importlib.__import__("sys")
                # Import builtins and replace import
                self.new_modules["builtins"] = importlib.__import__("builtins")
                self.new_modules["builtins"].__import__ = self.import_func  # type: ignore
                # Create a blank __main__ to help with anyone trying to
                # "import __main__" to not accidentally get the real main
                self.new_modules["__main__"] = types.ModuleType("__main__")
                # Reset sys modules again
                sys.modules = self.new_modules
            yield None
        finally:
            # sys.meta_path.remove(self)
            sys.modules = self.orig_modules
            builtins.__import__ = self.orig_import

    @contextmanager
    def _unapplied(self) -> Iterator[None]:
        # Error if already unapplied
        if sys.modules is not self.new_modules:
            raise RuntimeError("Sandbox importer already unapplied")

        # Set orig modules, then unset on complete
        sys.modules = self.orig_modules
        builtins.__import__ = self.orig_import
        try:
            yield None
        finally:
            # sys.meta_path.insert(0, self)
            sys.modules = self.new_modules
            builtins.__import__ = self.import_func

    def _traced_import(
        self,
        name: str,
        globals: Optional[Mapping[str, object]] = None,
        locals: Optional[Mapping[str, object]] = None,
        fromlist: Sequence[str] = (),
        level: int = 0,
    ) -> types.ModuleType:
        _trace("Importing %s (fromlist: %s, level: %s)", name, fromlist, level)
        global _trace_depth
        _trace_depth += 1
        try:
            return self._import(name, globals, locals, fromlist, level)
        finally:
            _trace_depth -= 1

    def _import(
        self,
        name: str,
        globals: Optional[Mapping[str, object]] = None,
        locals: Optional[Mapping[str, object]] = None,
        fromlist: Sequence[str] = (),
        level: int = 0,
    ) -> types.ModuleType:
        # Check module restrictions and passthrough modules
        if name not in sys.modules:
            # Make sure not an entirely invalid module
            self.env.assert_valid_module(name)

            # Check if passthrough
            with self._unapplied():
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
                    self.import_func(parent, globals, locals)
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

        return mod


class _Environment(Protocol):
    def assert_valid_module(self, name: str) -> None:
        ...

    def maybe_passthrough_module(self, name: str) -> Optional[types.ModuleType]:
        ...

    def maybe_restrict_module(
        self, mod: types.ModuleType
    ) -> Optional[types.ModuleType]:
        ...


class RestrictedEnvironment:
    def __init__(self, restrictions: SandboxRestrictions) -> None:
        self.restrictions = restrictions

    def assert_valid_module(self, name: str) -> None:
        if self.restrictions.invalid_modules.match_access(*name.split(".")):
            raise RestrictedWorkflowAccessError(name)

    def maybe_passthrough_module(self, name: str) -> Optional[types.ModuleType]:
        if not self.restrictions.passthrough_modules.match_access(*name.split(".")):
            return None
        _trace("Passing module %s through from host", name)
        global _trace_depth
        _trace_depth += 1
        # Use our import outside of the sandbox
        try:
            return importlib.import_module(name)
        finally:
            _trace_depth -= 1

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
