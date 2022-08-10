"""
@generated by mypy-protobuf.  Do not edit manually!
isort:skip_file
"""
import builtins
import google.protobuf.descriptor
import google.protobuf.duration_pb2
import google.protobuf.message
import google.protobuf.timestamp_pb2
import typing
import typing_extensions

DESCRIPTOR: google.protobuf.descriptor.FileDescriptor

class LockTimeSkippingRequest(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor
    def __init__(
        self,
    ) -> None: ...

global___LockTimeSkippingRequest = LockTimeSkippingRequest

class LockTimeSkippingResponse(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor
    def __init__(
        self,
    ) -> None: ...

global___LockTimeSkippingResponse = LockTimeSkippingResponse

class UnlockTimeSkippingRequest(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor
    def __init__(
        self,
    ) -> None: ...

global___UnlockTimeSkippingRequest = UnlockTimeSkippingRequest

class UnlockTimeSkippingResponse(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor
    def __init__(
        self,
    ) -> None: ...

global___UnlockTimeSkippingResponse = UnlockTimeSkippingResponse

class SleepUntilRequest(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor
    TIMESTAMP_FIELD_NUMBER: builtins.int
    @property
    def timestamp(self) -> google.protobuf.timestamp_pb2.Timestamp: ...
    def __init__(
        self,
        *,
        timestamp: typing.Optional[google.protobuf.timestamp_pb2.Timestamp] = ...,
    ) -> None: ...
    def HasField(
        self, field_name: typing_extensions.Literal["timestamp", b"timestamp"]
    ) -> builtins.bool: ...
    def ClearField(
        self, field_name: typing_extensions.Literal["timestamp", b"timestamp"]
    ) -> None: ...

global___SleepUntilRequest = SleepUntilRequest

class SleepRequest(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor
    DURATION_FIELD_NUMBER: builtins.int
    @property
    def duration(self) -> google.protobuf.duration_pb2.Duration: ...
    def __init__(
        self,
        *,
        duration: typing.Optional[google.protobuf.duration_pb2.Duration] = ...,
    ) -> None: ...
    def HasField(
        self, field_name: typing_extensions.Literal["duration", b"duration"]
    ) -> builtins.bool: ...
    def ClearField(
        self, field_name: typing_extensions.Literal["duration", b"duration"]
    ) -> None: ...

global___SleepRequest = SleepRequest

class SleepResponse(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor
    def __init__(
        self,
    ) -> None: ...

global___SleepResponse = SleepResponse

class GetCurrentTimeResponse(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor
    TIME_FIELD_NUMBER: builtins.int
    @property
    def time(self) -> google.protobuf.timestamp_pb2.Timestamp: ...
    def __init__(
        self,
        *,
        time: typing.Optional[google.protobuf.timestamp_pb2.Timestamp] = ...,
    ) -> None: ...
    def HasField(
        self, field_name: typing_extensions.Literal["time", b"time"]
    ) -> builtins.bool: ...
    def ClearField(
        self, field_name: typing_extensions.Literal["time", b"time"]
    ) -> None: ...

global___GetCurrentTimeResponse = GetCurrentTimeResponse
