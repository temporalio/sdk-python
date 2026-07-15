"""Error handling: retry classification and the workflow-failure type.

Two dep-free paths run against a real server: the HTTP-error → Temporal retry
translation, and that a raised :class:`DeepAgentsWorkflowError` surfaces to the
client as a non-retryable failure with a stable ``ApplicationError.type`` (never
a stringified peer exception). The model-instance validation error needs
LangChain and guards on its import.
"""

from __future__ import annotations

import sys
import uuid
from datetime import timedelta

import pytest

from temporalio.testing import WorkflowEnvironment

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="deepagents requires Python >= 3.11"
)
from temporalio import workflow
from temporalio.client import WorkflowFailureError
from temporalio.contrib._langchain._activity_helpers import (
    translate_api_error as _translate_api_error,
)
from temporalio.contrib.deepagents import DeepAgentsPlugin, DeepAgentsWorkflowError
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker


class _FakeResponse:
    def __init__(self, headers: dict) -> None:
        self.headers = headers


class _FakeHTTPError(Exception):
    def __init__(self, status_code: int, headers: dict | None = None) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.response = _FakeResponse(headers or {})


def test_error_classification() -> None:
    # 429 is retryable and honors the upstream Retry-After.
    err = _translate_api_error(_FakeHTTPError(429, {"retry-after": "7"}))
    assert isinstance(err, ApplicationError)
    assert err.non_retryable is False
    assert err.next_retry_delay == timedelta(seconds=7)

    # 400 is a client error: non-retryable.
    e400 = _translate_api_error(_FakeHTTPError(400))
    assert isinstance(e400, ApplicationError)
    assert e400.non_retryable is True

    # 503 is retryable by default...
    e503 = _translate_api_error(_FakeHTTPError(503))
    assert isinstance(e503, ApplicationError)
    assert e503.non_retryable is False
    # ...unless the server explicitly says not to.
    forced = _translate_api_error(_FakeHTTPError(503, {"x-should-retry": "false"}))
    assert isinstance(forced, ApplicationError)
    assert forced.non_retryable is True

    # retry-after-ms wins over retry-after when both are present.
    ms = _translate_api_error(
        _FakeHTTPError(429, {"retry-after-ms": "250", "retry-after": "7"})
    )
    assert isinstance(ms, ApplicationError)
    assert ms.next_retry_delay == timedelta(milliseconds=250)

    # A non-HTTP exception is not recognized, so the caller falls through.
    assert _translate_api_error(ValueError("nope")) is None


@workflow.defn
class FailingWorkflow:
    @workflow.run
    async def run(self) -> None:
        raise DeepAgentsWorkflowError("deliberate non-retryable failure")


@pytest.mark.asyncio
async def test_workflow_failure_type(env: WorkflowEnvironment) -> None:
    plugin = DeepAgentsPlugin()
    async with Worker(
        env.client,
        task_queue="da-fail",
        workflows=[FailingWorkflow],
        plugins=[plugin],
    ):
        handle = await env.client.start_workflow(
            FailingWorkflow.run,
            id=f"da-fail-{uuid.uuid4()}",
            task_queue="da-fail",
        )
        with pytest.raises(WorkflowFailureError) as excinfo:
            await handle.result()

    cause = excinfo.value.cause
    assert isinstance(cause, ApplicationError)
    assert cause.type == "deepagents.DeepAgentsWorkflowError"
    assert cause.non_retryable is True


@pytest.mark.asyncio
async def test_unwrappable_model_instance() -> None:
    pytest.importorskip("langchain_core")
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    # A string is auto-wrapped; a TemporalModel passes through; a live model
    # instance is rejected at the workflow boundary with the typed failure.
    from temporalio.contrib.deepagents import TemporalModel
    from temporalio.contrib.deepagents._model import _wrap_model_arg

    assert isinstance(_wrap_model_arg("anthropic:claude"), TemporalModel)
    tm = TemporalModel(model="anthropic:claude")
    assert _wrap_model_arg(tm) is tm
    with pytest.raises(DeepAgentsWorkflowError):
        _wrap_model_arg(FakeListChatModel(responses=["hi"]))
