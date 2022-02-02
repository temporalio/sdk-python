
from datetime import timedelta
from typing import Optional
import google.protobuf.duration_pb2


def optional_timedelta_to_duration(d: Optional[timedelta]) -> Optional[google.protobuf.duration_pb2.Duration]:
    if d is None:
        return None
    ret = google.protobuf.duration_pb2.Duration()
    ret.FromTimedelta(d)
    return ret