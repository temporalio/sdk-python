from datetime import timedelta

import pytest

from temporalio.common import RetryPolicy


def test_retry_policy_validate():
    # Validation ignored for max attempts as 1
    RetryPolicy(initial_interval=timedelta(seconds=-1), maximum_attempts=1)._validate()
    with pytest.raises(ValueError, match="Initial interval cannot be negative"):
        RetryPolicy(initial_interval=timedelta(seconds=-1))._validate()
    with pytest.raises(ValueError, match="Backoff coefficient cannot be less than 1"):
        RetryPolicy(backoff_coefficient=0.5)._validate()
    with pytest.raises(ValueError, match="Maximum interval cannot be negative"):
        RetryPolicy(maximum_interval=timedelta(seconds=-1))._validate()
    with pytest.raises(
        ValueError, match="Maximum interval cannot be less than initial interval"
    ):
        RetryPolicy(
            initial_interval=timedelta(seconds=3), maximum_interval=timedelta(seconds=1)
        )._validate()
    with pytest.raises(ValueError, match="Maximum attempts cannot be negative"):
        RetryPolicy(maximum_attempts=-1)._validate()
