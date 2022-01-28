from dataclasses import dataclass
from datetime import timedelta
from typing import Optional


@dataclass
class RetryPolicy:
    initial_interval: timedelta
    randomization_factor: float
    multiplier: float
    max_interval: timedelta
    max_elapsed_time: Optional[timedelta]
    max_retries: int
