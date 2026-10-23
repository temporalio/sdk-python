"""Run Temporal workers inside Modal Functions.

See the package README for a complete Workflow and Activity example.
"""

from temporalio.contrib.modal.worker._configure import ModalWorkerConfig
from temporalio.contrib.modal.worker._run_worker import run_worker

__all__ = [
    "ModalWorkerConfig",
    "run_worker",
]
