from .request_response_pb2 import LockTimeSkippingRequest
from .request_response_pb2 import LockTimeSkippingResponse
from .request_response_pb2 import UnlockTimeSkippingRequest
from .request_response_pb2 import UnlockTimeSkippingResponse
from .request_response_pb2 import SleepUntilRequest
from .request_response_pb2 import SleepRequest
from .request_response_pb2 import SleepResponse
from .request_response_pb2 import GetCurrentTimeResponse

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
    import grpc
    from .service_pb2_grpc import add_TestServiceServicer_to_server
    from .service_pb2_grpc import TestServiceStub
    from .service_pb2_grpc import TestServiceServicer
    __all__.extend(["TestServiceServicer", "TestServiceStub", "add_TestServiceServicer_to_server"])
except ImportError:
    pass