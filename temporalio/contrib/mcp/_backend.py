# pyright: reportUnusedClass=false

from __future__ import annotations

import inspect
from collections.abc import Callable
from types import TracebackType
from typing import Any, Protocol

from mcp.types import (
    CallToolResult,
    GetPromptResult,
    Prompt,
    ReadResourceResult,
    RequestParamsMeta,
    Resource,
    ResourceTemplate,
    Tool,
)

from temporalio.exceptions import ApplicationError


class _MCPBackend(Protocol):
    """Normalized worker-side MCP operations used by the Activity layer."""

    async def __aenter__(self) -> "_MCPBackend": ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None: ...

    @property
    def cacheable(self) -> bool: ...

    async def list_tools(self) -> list[Tool]: ...

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        meta: RequestParamsMeta | None,
    ) -> CallToolResult: ...

    async def list_prompts(self) -> list[Prompt]: ...

    async def get_prompt(
        self, name: str, arguments: dict[str, str] | None
    ) -> GetPromptResult: ...

    async def list_resources(self) -> list[Resource]: ...

    async def list_resource_templates(self) -> list[ResourceTemplate]: ...

    async def read_resource(self, uri: str) -> ReadResourceResult: ...


_MCPBackendFactory = Callable[[], _MCPBackend] | Callable[[Any], _MCPBackend]


class _NotSupplied:
    pass


_NOT_SUPPLIED = _NotSupplied()


def _factory_accepts_argument(name: str, factory: Callable[..., Any]) -> bool:
    """Resolve whether an MCP backend factory receives the ``factory_argument``.

    A factory declaring a positional parameter always receives the argument, as
    ``None`` when the workflow supplied none; a parameterless factory is always
    called with no arguments. A signature that could satisfy neither form raises
    here, so the mismatch surfaces where the factory is registered rather than as
    an Activity failure that retries forever.
    """
    try:
        parameters = list(inspect.signature(factory).parameters.values())
    except (TypeError, ValueError):
        # Some callables (C builtins, for instance) expose no signature. Assume
        # the parameterless form and let the call report any real mismatch.
        return False
    kinds = inspect.Parameter
    positional = [
        param
        for param in parameters
        if param.kind
        in (kinds.POSITIONAL_ONLY, kinds.POSITIONAL_OR_KEYWORD, kinds.VAR_POSITIONAL)
    ]
    required = [
        param
        for param in parameters
        if param.default is kinds.empty
        and param.kind not in (kinds.VAR_POSITIONAL, kinds.VAR_KEYWORD)
    ]
    if len(required) > 1:
        raise TypeError(
            f"MCP server factory {name!r} requires {len(required)} parameters; it "
            "may declare at most one, which receives the factory_argument"
        )
    if required and required[0].kind is kinds.KEYWORD_ONLY:
        raise TypeError(
            f"MCP server factory {name!r} requires keyword-only parameter "
            f"{required[0].name!r}, which cannot receive the factory_argument; "
            "make it positional or give it a default"
        )
    return bool(positional)


class _FactoryInvoker:
    """Call an MCP backend factory with the arity its signature declares."""

    def __init__(self, name: str, factory: Callable[..., Any]) -> None:
        self._name = name
        self._factory = factory
        self._accepts_argument = _factory_accepts_argument(name, factory)

    def __call__(self, argument: Any = _NOT_SUPPLIED) -> Any:
        supplied = None if isinstance(argument, _NotSupplied) else argument
        if self._accepts_argument:
            return self._factory(supplied)
        if supplied is not None:
            raise ApplicationError(
                f"MCP server factory {self._name!r} declares no parameters, so it "
                "cannot receive a factory_argument; give the factory a single "
                "positional parameter to accept one",
                non_retryable=True,
            )
        return self._factory()
