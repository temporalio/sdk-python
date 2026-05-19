"""Failure converter that preserves Strands ``InterruptException`` payloads."""

from strands.interrupt import InterruptException

import temporalio.api.failure.v1
from temporalio.converter import DefaultFailureConverter, PayloadConverter
from temporalio.exceptions import ApplicationError

# Activity-side: when a Strands ``InterruptException`` would otherwise be
# serialized by the default converter, the ``Interrupt`` payload on
# ``exc.interrupt`` is dropped (it lives on the instance, not in the
# serialized ApplicationError). We translate to a typed ApplicationError so
# the interrupt data survives the activity boundary and the workflow side
# can rebuild a real ``Interrupt``.
STRANDS_INTERRUPT_TYPE = "StrandsInterrupt"


class StrandsFailureConverter(DefaultFailureConverter):
    """Failure converter that preserves Strands ``InterruptException`` payloads."""

    def to_failure(
        self,
        exception: BaseException,
        payload_converter: PayloadConverter,
        failure: temporalio.api.failure.v1.Failure,
    ) -> None:
        """Translate ``InterruptException`` to a typed ``ApplicationError``."""
        if isinstance(exception, InterruptException):
            super().to_failure(
                ApplicationError(
                    f"interrupt:{exception.interrupt.name}",
                    exception.interrupt.to_dict(),
                    type=STRANDS_INTERRUPT_TYPE,
                    non_retryable=True,
                ),
                payload_converter,
                failure,
            )
            return
        super().to_failure(exception, payload_converter, failure)
