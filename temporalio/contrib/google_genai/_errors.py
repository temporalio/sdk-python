"""Error types for the Google Gemini SDK Temporal integration."""

from __future__ import annotations

from temporalio.exceptions import ApplicationError


class GoogleGenAIError(ApplicationError):
    """Error raised by the Google Gemini Temporal integration.

    Registered with the worker (and replayer) via
    ``workflow_failure_exception_types`` so that, when raised in workflow code,
    it terminally fails the workflow execution rather than failing the workflow
    task and retrying it indefinitely.  Use it for conditions that cannot be
    recovered by retry — e.g. a tool that is not a valid Temporal activity.
    """
