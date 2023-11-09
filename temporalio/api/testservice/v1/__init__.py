from .request_response_pb2 import (
    GetCurrentTimeResponse,
    LockTimeSkippingRequest,
    LockTimeSkippingResponse,
    SleepRequest,
    SleepResponse,
    SleepUntilRequest,
    UnlockTimeSkippingRequest,
    UnlockTimeSkippingResponse,
)

__all__ = [
    "GetCurrentTimeResponse",
    "LockTimeSkippingRequest",
    "LockTimeSkippingResponse",
    "SleepRequest",
    "SleepResponse",
    "SleepUntilRequest",
    "UnlockTimeSkippingRequest",
    "UnlockTimeSkippingResponse",
]

# gRPC is optional
try:
    __all__.extend(
        ["TestServiceServicer", "TestServiceStub", "add_TestServiceServicer_to_server"]
    )
except ImportError:
    pass
