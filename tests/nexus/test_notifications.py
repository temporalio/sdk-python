from __future__ import annotations

import dataclasses
from typing import cast

import pytest

import temporalio.converter
import temporalio.exceptions
import temporalio.nexus.notifications as notifications
import temporalio.nexus.notifications.models as models
import temporalio.nexus.system as nexus_system


@dataclasses.dataclass
class NotificationValue:
    message: str


@pytest.mark.parametrize("success", [True, False])
def test_notification_request_roundtrip(success: bool) -> None:
    data_converter = temporalio.converter.default()
    converter = nexus_system._get_payload_converter(
        data_converter.payload_converter, data_converter.failure_converter
    )
    result: models.OnCompleteRequestResult[NotificationValue] = (
        models.OnCompleteRequestResultSuccess(NotificationValue("result"))
        if success
        else models.OnCompleteRequestResultFailure(ValueError("failure"))
    )
    request = notifications.OnCompleteRequest(
        result=result, source_context=NotificationValue("context")
    )
    payload = converter.to_payload(request)
    decoded = cast(
        notifications.OnCompleteRequest[NotificationValue, NotificationValue],
        converter.from_payload(
            payload,
            notifications.OnCompleteRequest[NotificationValue, NotificationValue],
        ),
    )
    assert decoded.source_context == request.source_context
    assert isinstance(decoded.source_context, NotificationValue)
    if success:
        assert isinstance(decoded.result, models.OnCompleteRequestResultSuccess)
        assert decoded.result.value == NotificationValue("result")
        assert isinstance(decoded.result.value, NotificationValue)
    else:
        assert isinstance(decoded.result, models.OnCompleteRequestResultFailure)
        assert isinstance(decoded.result.value, temporalio.exceptions.ApplicationError)
        assert decoded.result.value.message == "failure"
        assert decoded.result.value.type == "ValueError"


def test_notification_response_roundtrip() -> None:
    data_converter = temporalio.converter.default()
    converter = nexus_system._get_payload_converter(
        data_converter.payload_converter, data_converter.failure_converter
    )
    response = notifications.OnCompleteResponse()
    assert (
        converter.from_payload(
            converter.to_payload(response), notifications.OnCompleteResponse
        )
        == response
    )
