import nexusrpc
import pytest

import temporalio.client._impl
import temporalio.worker._nexus


@pytest.mark.parametrize(
    "message",
    [
        "Activity must have start_to_close_timeout or schedule_to_close_timeout",
        "start_delay must be non-negative",
    ],
)
def test_start_activity_input_error_maps_to_nexus_bad_request(message: str):
    """Pins the classification `_exception_to_handler_error` relies on to give
    these two `start_activity` preconditions non-retryable BAD_REQUEST
    semantics inside a Nexus operation handler, instead of falling through to
    the generic, retryable INTERNAL branch.
    """
    handler_error = temporalio.worker._nexus._exception_to_handler_error(
        temporalio.client._impl._StartActivityInputError(message)
    )
    assert handler_error.type == nexusrpc.HandlerErrorType.BAD_REQUEST
    assert handler_error.retryable is False
    assert handler_error.message == message
