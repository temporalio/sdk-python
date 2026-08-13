"""Sandbox environment value resolved from the Temporal Worker's environment."""

from __future__ import annotations

import os
from typing import Literal

from agents.sandbox.manifest import EnvValue

from temporalio import workflow
from temporalio.exceptions import ApplicationError


class TemporalWorkerEnvValue(EnvValue):
    """A sandbox environment variable whose value is read on the Temporal Worker.

    .. warning::
        This class is experimental and may change in future versions.
        Use with caution in production environments.

    Use it wherever a sandbox environment value should come from the worker's
    environment rather than being written into the manifest. It carries the name
    of the variable, and the worker reads the value when the sandbox environment
    is needed. Only the name is recorded in workflow history.

    ::

        from agents.sandbox import Manifest
        from agents.sandbox.manifest import Environment

        from temporalio.contrib.openai_agents import TemporalWorkerEnvValue

        manifest = Manifest(
            environment=Environment(
                value={
                    "OPENAI_API_KEY": TemporalWorkerEnvValue(key="OPENAI_API_KEY"),
                    "REGION": "us-west-2",
                }
            )
        )

    Set the variable on every worker that runs sandbox activities. The two
    names need not match: ``{"OPENAI_API_KEY": TemporalWorkerEnvValue(key="PROD_KEY")}``
    reads ``PROD_KEY`` on the worker and sets ``OPENAI_API_KEY`` inside the
    sandbox.
    """

    type: Literal["temporal.worker_env_value"] = "temporal.worker_env_value"  # type: ignore[assignment]
    """Discriminator for this environment value type."""

    key: str
    """Name of the environment variable to read on the worker."""

    async def resolve(self) -> str:
        """Return the value read from the worker's environment.

        Raises:
            ApplicationError: If :py:attr:`key` is unset or empty, or if called
                from workflow code. Non-retryable, with
                ``type="TemporalWorkerEnvValueUnresolved"``.
        """
        if workflow.in_workflow():
            raise ApplicationError(
                "TemporalWorkerEnvValue.resolve() must run on a worker, not in workflow "
                "code: it reads the process environment, which is non-deterministic on "
                "replay and would pull the value into workflow state.",
                type="TemporalWorkerEnvValueUnresolved",
                non_retryable=True,
            )
        value = os.environ.get(self.key)
        if not value:
            raise ApplicationError(
                f"TemporalWorkerEnvValue environment variable {self.key!r} is not set, "
                "or is empty, in the worker process environment.",
                type="TemporalWorkerEnvValueUnresolved",
                non_retryable=True,
            )
        return value
