"""
@generated by mypy-protobuf.  Do not edit manually!
isort:skip_file
"""

import builtins
import sys

import google.protobuf.descriptor
import google.protobuf.duration_pb2
import google.protobuf.message

if sys.version_info >= (3, 8):
    import typing as typing_extensions
else:
    import typing_extensions

DESCRIPTOR: google.protobuf.descriptor.FileDescriptor

class SomeMessage(google.protobuf.message.Message):
    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    SOME_DURATION_FIELD_NUMBER: builtins.int
    @property
    def some_duration(self) -> google.protobuf.duration_pb2.Duration: ...
    def __init__(
        self,
        *,
        some_duration: google.protobuf.duration_pb2.Duration | None = ...,
    ) -> None: ...
    def HasField(
        self, field_name: typing_extensions.Literal["some_duration", b"some_duration"]
    ) -> builtins.bool: ...
    def ClearField(
        self, field_name: typing_extensions.Literal["some_duration", b"some_duration"]
    ) -> None: ...

global___SomeMessage = SomeMessage
