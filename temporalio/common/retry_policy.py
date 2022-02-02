from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

import temporalio.api.common.v1


@dataclass
class RetryPolicy:
    initial_interval: timedelta
    randomization_factor: float
    multiplier: float
    max_interval: timedelta
    max_elapsed_time: Optional[timedelta]
    max_retries: int

    def to_proto(self) -> temporalio.api.common.v1.RetryPolicy:
        raise NotImplementedError