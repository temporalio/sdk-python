from .message_pb2 import S3Spec
from .message_pb2 import GCSSpec
from .message_pb2 import KinesisSpec
from .message_pb2 import PubSubSpec

__all__ = [
    "GCSSpec",
    "KinesisSpec",
    "PubSubSpec",
    "S3Spec",
]
