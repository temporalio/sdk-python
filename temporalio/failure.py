from enum import IntEnum
from typing import Any, List, Optional

import temporalio.api.enums.v1
import temporalio.api.failure.v1
import temporalio.converter


class FailureError(Exception):
    @staticmethod
    async def from_proto(
        failure: temporalio.api.failure.v1.Failure,
        data_converter: temporalio.converter.DataConverter,
    ) -> "FailureError":
        raise NotImplementedError

    def __init__(self, message: str, failure: "Failure") -> None:
        super().__init__(message)
        raise NotImplementedError


class Failure:
    def __init__(
        self,
        *details: Any,
        proto_failure: Optional[temporalio.api.failure.v1.Failure] = None
    ) -> None:
        raise NotImplementedError


class CancelledFailure(Failure):
    def __init__(self, *details: Any) -> None:
        super().__init__(*details)
        raise NotImplementedError


class TerminatedFailure(Failure):
    def __init__(self, *details: Any, reason: Optional[str]) -> None:
        super().__init__(*details)
        raise NotImplementedError


class TimeoutType(IntEnum):
    START_TO_CLOSE = int(
        temporalio.api.enums.v1.TimeoutType.TIMEOUT_TYPE_START_TO_CLOSE
    )
    SCHEDULE_TO_START = int(
        temporalio.api.enums.v1.TimeoutType.TIMEOUT_TYPE_SCHEDULE_TO_START
    )
    SCHEDULE_TO_CLOSE = int(
        temporalio.api.enums.v1.TimeoutType.TIMEOUT_TYPE_SCHEDULE_TO_CLOSE
    )
    HEARTBEAT = int(temporalio.api.enums.v1.TimeoutType.TIMEOUT_TYPE_HEARTBEAT)


class TimeoutFailure(Failure):
    def __init__(
        self, type: TimeoutType, last_heartbeat_details: Optional[List[Any]] = None
    ) -> None:
        super().__init__()
        raise NotImplementedError
