"""Temporal-specific integration for Workspace."""

from __future__ import annotations

import contextvars
import functools
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import temporalio.activity

from temporalio.contrib.workdir._workspace import Workspace

F = TypeVar("F", bound=Callable[..., Any])

_current_workspace_path: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "_current_workspace_path", default=None
)


def workspace(
    remote_url_template: str,
    key_fn: Callable[..., dict[str, str]] | None = None,
    **workspace_kwargs: Any,
) -> Callable[[F], F]:
    """Decorator that provides a :class:`Workspace` to a Temporal activity.

    The workspace path is available via :func:`get_workspace_path` inside
    the activity body. The workspace is pulled before execution and pushed
    after successful completion.

    Template variables in ``remote_url_template`` are resolved from
    :func:`temporalio.activity.info` and, optionally, from ``key_fn``.

    Built-in template variables (from ``activity.info()``):

    - ``{workflow_id}``
    - ``{workflow_run_id}``
    - ``{activity_id}``
    - ``{activity_type}``
    - ``{task_queue}``

    Example::

        @workspace("gs://bucket/{workflow_id}/{activity_type}")
        @activity.defn
        async def process(input: ProcessInput) -> Output:
            ws = get_workspace_path()
            data = json.loads((ws / "config.json").read_text())
            ...

        # With key_fn for custom template vars:
        @workspace(
            "gs://bucket/{workflow_id}/{component}",
            key_fn=lambda input: {"component": input.component_name},
        )
        @activity.defn
        async def process(input: ProcessInput) -> Output:
            ...

    Args:
        remote_url_template: URL template with ``{var}`` placeholders.
        key_fn: Optional function that receives the activity's positional
            arguments and returns a dict of additional template variables.
        **workspace_kwargs: Extra keyword arguments forwarded to
            :class:`Workspace` (e.g., ``cleanup="keep"``).
    """

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            info = temporalio.activity.info()
            template_vars: dict[str, str] = {
                "workflow_id": info.workflow_id,
                "workflow_run_id": info.workflow_run_id,
                "activity_id": info.activity_id,
                "activity_type": info.activity_type,
                "task_queue": info.task_queue,
            }
            if key_fn is not None:
                template_vars.update(key_fn(*args))

            remote_url = remote_url_template.format(**template_vars)

            async with Workspace(remote_url, **workspace_kwargs) as ws:
                token = _current_workspace_path.set(ws.path)
                try:
                    return await fn(*args, **kwargs)
                finally:
                    _current_workspace_path.reset(token)

        return wrapper  # type: ignore[return-value]

    return decorator


def get_workspace_path() -> Path:
    """Get the workspace path for the currently executing activity.

    Call this from inside an activity decorated with :func:`workspace`.

    Returns:
        The local workspace :class:`~pathlib.Path`.

    Raises:
        RuntimeError: If called outside a workspace-decorated activity.
    """
    path = _current_workspace_path.get(None)
    if path is None:
        raise RuntimeError(
            "get_workspace_path() called outside a workspace-decorated activity"
        )
    return path
