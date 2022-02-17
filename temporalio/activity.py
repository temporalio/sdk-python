from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Optional


@dataclass(frozen=True)
class Info:
    activity_id: str
    activity_type: str
    attempt: int
    heartbeat_details: Iterable[Any]
    heartbeat_timeout: Optional[timedelta]
    schedule_to_close_timeout: Optional[timedelta]
    scheduled_time: datetime
    start_to_close_timeout: Optional[timedelta]
    started_time: datetime
    task_queue: str
    task_token: bytes
    workflow_id: str
    workflow_namespace: str
    workflow_run_id: str
    workflow_type: str


@dataclass
class Context:
    def __init__(
        self, info: Callable[[], Info], heartbeat: Callable[..., None]
    ) -> None:
        self._info = info
        self._heartbeat = heartbeat

    def info(self) -> Info:
        return self._info()

    def heartbeat(self, *details: Any) -> None:
        self._heartbeat(*details)


def current() -> Context:
    pass
