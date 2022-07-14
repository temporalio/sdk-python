import logging
import logging.handlers
import traceback
from typing import Optional

import temporalio.converter
from temporalio.api.failure.v1 import Failure
from temporalio.exceptions import (
    ApplicationError,
    FailureError,
    apply_exception_to_failure,
    failure_to_error,
)


# This is an example of appending the stack to every Temporal failure error
def append_temporal_stack(exc: Optional[BaseException]) -> None:
    while exc:
        # Only append if it doesn't appear already there
        if (
            isinstance(exc, FailureError)
            and exc.failure
            and exc.failure.stack_trace
            and len(exc.args) == 1
            and "\nStack:\n" not in exc.args[0]
        ):
            exc.args = (f"{exc}\nStack:\n{exc.failure.stack_trace.rstrip()}",)
        exc = exc.__cause__


def test_exception_format():
    # Cause a nested exception
    actual_err: Exception
    try:
        try:
            raise ValueError("error1")
        except Exception as err:
            raise RuntimeError("error2") from err
    except Exception as err:
        actual_err = err
    assert actual_err

    # Convert to failure and back
    failure = Failure()
    apply_exception_to_failure(
        actual_err, temporalio.converter.default().payload_converter, failure
    )
    print("FAILURE", failure)
    failure_error = failure_to_error(
        failure, temporalio.converter.default().payload_converter
    )
    # Confirm type is prepended
    assert isinstance(failure_error, ApplicationError)
    assert "RuntimeError: error2" == str(failure_error)
    assert isinstance(failure_error.cause, ApplicationError)
    assert "ValueError: error1" == str(failure_error.cause)

    # Append the stack and format the exception and check the output
    append_temporal_stack(failure_error)
    output = "".join(traceback.format_exception(failure_error))
    assert "temporalio.exceptions.ApplicationError: ValueError: error1" in output
    assert "temporalio.exceptions.ApplicationError: RuntimeError: error" in output
    assert output.count("\nStack:\n") == 2

    # This shows how it might look for those with debugging on
    logging.getLogger(__name__).debug(
        "Showing appended exception", exc_info=failure_error
    )
