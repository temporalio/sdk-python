"""References to environment variables resolved by sandbox activity workers."""

from __future__ import annotations

import os
import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import cast

_REF_PREFIX = "temporal.worker_env_ref:"
_REF_PATTERN = re.compile(re.escape(_REF_PREFIX) + r"\{([^}{]*)\}")


@dataclass(frozen=True)
class AllowAllWorkerEnvVars:
    """Make every environment variable on the worker resolvable.

    .. warning::
        This class is experimental and may change in future versions.
        Use with caution in production environments.

    Pass an instance in place of a list of names::

        StrandsPlugin(resolvable_worker_env_vars=AllowAllWorkerEnvVars())

    This lets workflow code name any variable on the worker and make its value
    available to commands in the sandbox.
    """


def temporal_worker_env_ref(name: str) -> str:
    """Refer to an environment variable held by a sandbox activity worker.

    .. warning::
        This function is experimental and may change in future versions.
        Use with caution in production environments.

    The returned string contains only the variable name. A worker resolves it
    immediately before invoking the sandbox when the name is allowed by
    ``StrandsPlugin(resolvable_worker_env_vars=...)``.
    """
    return f"{_REF_PREFIX}{{{name}}}"


class _WorkerEnvRefResolver:  # type:ignore[reportUnusedClass]
    def __init__(
        self,
        resolvable_worker_env_vars: Collection[str] | AllowAllWorkerEnvVars,
    ) -> None:
        if isinstance(resolvable_worker_env_vars, AllowAllWorkerEnvVars):
            self._allowed: frozenset[str] | AllowAllWorkerEnvVars = (
                resolvable_worker_env_vars
            )
        elif isinstance(resolvable_worker_env_vars, str):
            raise TypeError(
                "resolvable_worker_env_vars takes a collection of environment "
                'variable names, such as ["MY_API_KEY"], or '
                "AllowAllWorkerEnvVars(). A single string is read as the collection "
                "of its characters, so pass a list even for one name."
            )
        elif cast(object, resolvable_worker_env_vars) is AllowAllWorkerEnvVars:
            raise TypeError(
                "resolvable_worker_env_vars takes an AllowAllWorkerEnvVars instance, "
                "not the class itself. Pass AllowAllWorkerEnvVars()."
            )
        else:
            self._allowed = frozenset(resolvable_worker_env_vars)

    def resolve(self, env: dict[str, str] | None) -> dict[str, str] | None:
        """Return a copy with allowed worker environment references resolved."""
        if env is None:
            return None
        return {name: self._resolve_value(value) for name, value in env.items()}

    def _resolve_value(self, value: str) -> str:
        def substitute(match: re.Match[str]) -> str:
            name = match.group(1)
            if not (
                isinstance(self._allowed, AllowAllWorkerEnvVars)
                or name in self._allowed
            ):
                return match.group(0)
            return os.environ.get(name, "")

        return _REF_PATTERN.sub(substitute, value)
