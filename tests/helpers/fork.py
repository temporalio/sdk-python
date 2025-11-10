import asyncio
import multiprocessing
import multiprocessing.context
from dataclasses import dataclass
from typing import Any, Self

import pytest


@dataclass
class _ForkTestResult:
    status: str
    err_name: str | None
    err_msg: str | None

    def __eq__(self, value: object) -> bool:
        if not isinstance(value, _ForkTestResult):
            return False

        valid_err_msg = False

        if self.err_msg and value.err_msg:
            valid_err_msg = (
                self.err_msg in value.err_msg or value.err_msg in self.err_msg
            )

        return (
            value.status == self.status
            and value.err_name == value.err_name
            and valid_err_msg
        )

    @classmethod
    def assertion_error(cls, message: str) -> Self:
        return cls(status="error", err_name="AssertionError", err_msg=message)


class _TestFork:
    _expected: _ForkTestResult

    async def coro(self) -> Any:
        raise NotImplementedError()

    def entry(self):
        event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(event_loop)
        try:
            event_loop.run_until_complete(self.coro())
            payload = _ForkTestResult(status="ok", err_name=None, err_msg=None)
        except BaseException as err:
            payload = _ForkTestResult(
                status="error", err_name=err.__class__.__name__, err_msg=str(err)
            )

        self._child_conn.send(payload)
        self._child_conn.close()

    def run(self, mp_fork_context: multiprocessing.context.ForkContext | None):
        if not mp_fork_context:
            pytest.skip("fork context not available")

        self._parent_conn, self._child_conn = mp_fork_context.Pipe(duplex=False)
        # start fork
        child_process = mp_fork_context.Process(
            target=self.entry, args=(), daemon=False
        )
        child_process.start()
        # close parent's handle on child_conn
        self._child_conn.close()

        # get run info from pipe
        payload = self._parent_conn.recv()
        self._parent_conn.close()

        assert payload == self._expected
