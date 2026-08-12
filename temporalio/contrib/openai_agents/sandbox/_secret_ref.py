"""Reference to a secret held in the worker process environment."""

from __future__ import annotations

import os
from typing import Literal

from agents.sandbox.manifest import EnvValue

from temporalio import workflow
from temporalio.exceptions import ApplicationError


class SecretRef(EnvValue):
    """A sandbox environment variable whose value is read on the worker.

    .. warning::
        This class is experimental and may change in future versions.
        Use with caution in production environments.

    Use it in place of the value, so only the variable's name reaches
    workflow history::

        from agents.sandbox import Manifest
        from agents.sandbox.manifest import Environment

        from temporalio.contrib.openai_agents import SecretRef

        manifest = Manifest(
            environment=Environment(
                value={
                    "OPENAI_API_KEY": SecretRef(key="OPENAI_API_KEY"),
                    "REGION": "us-west-2",
                }
            )
        )

    Set the variable on every worker that runs sandbox activities. The two
    names need not match: ``{"OPENAI_API_KEY": SecretRef(key="PROD_KEY")}``
    reads ``PROD_KEY`` on the worker and sets ``OPENAI_API_KEY`` inside the
    sandbox.
    """

    type: Literal["temporal.secret_ref"] = "temporal.secret_ref"  # type: ignore[assignment]
    """Discriminator for this environment value type."""

    key: str
    """Name of the environment variable to read on the worker."""

    async def resolve(self) -> str:
        """Return the secret read from the worker's environment.

        Raises:
            ApplicationError: If :py:attr:`key` is unset or empty, or if called
                from workflow code. Non-retryable.
        """
        if workflow.in_workflow():
            raise ApplicationError(
                "SecretRef.resolve() must run on a worker, not in workflow code: it "
                "reads the process environment, which is non-deterministic on replay "
                "and would pull the secret into workflow state.",
                type="SecretRefUnusable",
                non_retryable=True,
            )
        value = os.environ.get(self.key)
        if not value:
            raise ApplicationError(
                f"SecretRef environment variable {self.key!r} is not set, or is "
                "empty, in the worker process environment.",
                type="SecretRefUnusable",
                non_retryable=True,
            )
        return value
