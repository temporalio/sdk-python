"""Payload validation error helpers."""

from typing import Any

import temporalio.exceptions


def create_payload_validation_error(
    details: Any,
) -> temporalio.exceptions.ApplicationError:
    """Create an error indicating that a converted payload failed validation.

    Args:
        details: Structured details describing the validation failure.

    Returns:
        A non-retryable application error with the reserved payload validation
        failure type.
    """
    return temporalio.exceptions.ApplicationError(
        "Payload validation failed",
        details,
        type="PayloadValidationError",
        non_retryable=True,
    )
