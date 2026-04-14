"""Temporal SimplePlugin integration for Tenuo warrant-based authorization.

Provides :class:`TenuoPlugin` which registers client + worker interceptors and
configures the workflow sandbox passthrough for ``tenuo`` / ``tenuo_core``
(PyO3 native extension).

Example::

    from temporalio.client import Client
    from temporalio.contrib.tenuo import TenuoPlugin
    from tenuo.temporal import TenuoPluginConfig, EnvKeyResolver

    plugin = TenuoPlugin(TenuoPluginConfig(
        key_resolver=EnvKeyResolver(),
        trusted_roots=[issuer_pubkey],
    ))
    client = await Client.connect("localhost:7233", plugins=[plugin])
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from tenuo.temporal import (
    TenuoClientInterceptor,
    TenuoPlugin as _TenuoWorkerInterceptor,
    TenuoPluginConfig,
    build_activity_registry,
    set_worker_config,
    tenuo_internal_mint_activity,
)

from temporalio.plugin import SimplePlugin

if TYPE_CHECKING:
    from temporalio.worker import WorkflowRunner

_logger = logging.getLogger("tenuo.temporal")

TENUO_PLUGIN_NAME = "tenuo.TenuoTemporalPlugin"


def ensure_tenuo_workflow_runner(
    existing: WorkflowRunner | None,
) -> WorkflowRunner:
    """Return a workflow runner with ``tenuo`` and ``tenuo_core`` sandbox passthrough.

    Args:
        existing: The current workflow runner, or ``None``.

    Returns:
        A workflow runner with tenuo passthrough modules added.
    """
    from temporalio.worker.workflow_sandbox import (
        SandboxedWorkflowRunner,
        SandboxRestrictions,
    )

    passthrough = ("tenuo", "tenuo_core")
    if existing is None:
        return SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules(
                *passthrough
            )
        )
    if isinstance(existing, SandboxedWorkflowRunner):
        return dataclasses.replace(
            existing,
            restrictions=existing.restrictions.with_passthrough_modules(*passthrough),
        )
    return existing


class TenuoPlugin(SimplePlugin):
    """Temporal plugin for Tenuo warrant-based authorization and PoP enforcement.

    Configures:

    - **Client:** :class:`~tenuo.temporal.TenuoClientInterceptor` for warrant
      header injection and workflow-ID binding.
    - **Worker:** Worker interceptors for outbound PoP signing and inbound
      activity authorization.
    - **Workflow runner:** Sandbox passthrough for ``tenuo`` and ``tenuo_core``.

    After construction, :attr:`client_interceptor` provides the interceptor
    instance registered on the client.

    Example::

        from temporalio.client import Client
        from temporalio.worker import Worker
        from temporalio.contrib.tenuo import TenuoPlugin
        from tenuo.temporal import TenuoPluginConfig, EnvKeyResolver

        plugin = TenuoPlugin(TenuoPluginConfig(
            key_resolver=EnvKeyResolver(),
            trusted_roots=[issuer_pubkey],
        ))

        client = await Client.connect("localhost:7233", plugins=[plugin])
        async with Worker(
            client,
            task_queue="my-queue",
            workflows=[MyWorkflow],
            activities=[my_activity],
        ):
            pass

    .. warning::

        Register this plugin on ``Client.connect(plugins=[plugin])`` only.
        Workers built from that client inherit the plugin automatically.
        Passing the same instance to ``Worker(plugins=[...])`` double-registers
        interceptors.
    """

    client_interceptor: TenuoClientInterceptor
    """The client interceptor wired into this plugin."""

    def __init__(
        self,
        config: TenuoPluginConfig,
        *,
        client_interceptor: TenuoClientInterceptor | None = None,
    ) -> None:
        """Create a Tenuo plugin with the given configuration.

        Args:
            config: Worker-level configuration for authorization enforcement.
            client_interceptor: Optional pre-existing client interceptor. If
                ``None``, a new one is created.
        """
        worker_interceptor = _TenuoWorkerInterceptor(config)
        self.client_interceptor = client_interceptor or TenuoClientInterceptor()
        self._tenuo_config = config
        self._tenuo_worker_configured = False

        def _add_activities(
            activities: Sequence[Callable[..., Any]] | None,
        ) -> Sequence[Callable[..., Any]]:
            """Append internal activities and auto-discover activity_fns."""
            if self._tenuo_worker_configured:
                raise RuntimeError(
                    "Duplicate Tenuo plugin registration: the same TenuoPlugin "
                    "instance was used to configure_worker more than once. "
                    "Create separate instances for each worker."
                )
            self._tenuo_worker_configured = True

            set_worker_config(config)
            existing = list(activities or [])

            if not config.activity_fns and existing:
                config.activity_fns = list(existing)
                config._activity_registry = build_activity_registry(config.activity_fns)
                _logger.info(
                    "Tenuo: auto-discovered %d activity function(s)",
                    len(config.activity_fns),
                )

            _preload_all = getattr(config.key_resolver, "preload_all", None)
            if _preload_all is not None:
                try:
                    _preload_all()
                except Exception as exc:
                    _logger.warning("key preload failed: %s", exc)

            if tenuo_internal_mint_activity is not None:
                existing.append(tenuo_internal_mint_activity)
            return existing

        super().__init__(
            TENUO_PLUGIN_NAME,
            workflow_runner=ensure_tenuo_workflow_runner,
            activities=_add_activities,
            interceptors=[self.client_interceptor, worker_interceptor],
        )
