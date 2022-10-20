from __future__ import annotations

import builtins
import functools
import importlib
import logging
import sys
import types
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence, Set, Tuple

from typing_extensions import Protocol

from .restrictions import (
    RestrictedModule,
    RestrictedWorkflowAccessError,
    RestrictionContext,
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

    # TODO(cretz): Document that this is expected to have a global lock
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

            # Restrict builtins
            self.env.restrict_builtins()

            yield None
        finally:
            sys.modules = self.orig_modules
            builtins.__import__ = self.orig_import
            self.env.unrestrict_builtins()

    @contextmanager
    def _unapplied(self) -> Iterator[None]:
        # Error if already unapplied
        if sys.modules is not self.new_modules:
            raise RuntimeError("Sandbox importer already unapplied")

        # Set orig modules, then unset on complete
        sys.modules = self.orig_modules
        builtins.__import__ = self.orig_import
        self.env.unrestrict_builtins()
        try:
            yield None
        finally:
            sys.modules = self.new_modules
            builtins.__import__ = self.import_func
            self.env.restrict_builtins()

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

    def restrict_builtins(self) -> None:
        ...

    def unrestrict_builtins(self) -> None:
        ...


# TODO(cretz): Document that this is stateless
class RestrictedEnvironment:
    def __init__(
        self, restrictions: SandboxRestrictions, context: RestrictionContext
    ) -> None:
        self.restrictions = restrictions
        self.context = context

        # Pre-build restricted builtins as tuple of applied and unapplied.
        # Python doesn't allow us to wrap the builtins dictionary with our own
        # __getitem__ type (low-level C assertion is performed to confirm it is
        # a dict), so we instead choose to walk the builtins children and
        # specifically set them as not callable.
        self.restricted_builtins: Dict[str, Tuple[Any, Any]] = {}
        builtin_matcher = restrictions.invalid_module_members.child_matcher(
            "__builtins__"
        )
        if builtin_matcher:

            def restrict_built_in(name: str, orig: Any, *args, **kwargs):
                # Check if restricted against matcher
                if builtin_matcher and builtin_matcher.match_access(
                    context, name, include_use=True
                ):
                    raise RestrictedWorkflowAccessError(f"__builtins__.{name}")
                return orig(*args, **kwargs)

            assert isinstance(__builtins__, dict)
            # Only apply to functions that are anywhere in access, use, or
            # children
            for k, v in __builtins__.items():
                if (
                    k in builtin_matcher.access
                    or k in builtin_matcher.use
                    or k in builtin_matcher.children
                ):
                    _trace("Restricting builtin %s", k)
                    self.restricted_builtins[k] = (
                        functools.partial(restrict_built_in, k, v),
                        v,
                    )

    def assert_valid_module(self, name: str) -> None:
        if self.restrictions.invalid_modules.match_access(
            self.context, *name.split(".")
        ):
            raise RestrictedWorkflowAccessError(name)

    def maybe_passthrough_module(self, name: str) -> Optional[types.ModuleType]:
        if not self.restrictions.passthrough_modules.match_access(
            self.context, *name.split(".")
        ):
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
        return RestrictedModule(mod, self.context, matcher)

    def restrict_builtins(self) -> None:
        for k, v in self.restricted_builtins.items():
            setattr(builtins, k, v[0])

    def unrestrict_builtins(self) -> None:
        for k, v in self.restricted_builtins.items():
            setattr(builtins, k, v[1])
