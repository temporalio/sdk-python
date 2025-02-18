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
    import grpc  # noqa # type: ignore

    from .service_pb2_grpc import (  # noqa: F401
        TestServiceServicer,
        TestServiceStub,
        add_TestServiceServicer_to_server,
    )

    __all__.extend(
        ["TestServiceServicer", "TestServiceStub", "add_TestServiceServicer_to_server"]
    )
except ImportError:
    pass
