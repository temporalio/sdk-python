from collections.abc import AsyncIterable
from datetime import timedelta
from typing import Any
from uuid import uuid4

from strands.models import Model
from strands.types.streaming import StreamEvent

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.strands import StrandsPlugin, TemporalAgent
from temporalio.worker import Worker

# Worker-side sink: the recording model writes the invocation_state it
# received here so the test body can inspect it after the workflow completes.
_RECEIVED: list[dict[str, Any]] = []


class _RecordingModel(Model):
    def update_config(self, **_model_config: Any) -> None:
        return None

    def get_config(self) -> dict[str, Any]:
        return {}

    def structured_output(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError

    async def stream(
        self,
        *_args: Any,
        invocation_state: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        _RECEIVED.append(invocation_state or {})
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockDelta": {"delta": {"text": "ok"}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


@workflow.defn
class _InvocationStateWorkflow:
    def __init__(self) -> None:
        self.agent = TemporalAgent(
            model="recording",
            start_to_close_timeout=timedelta(seconds=15),
        )

    @workflow.run
    async def run(self, prompt: str) -> str:
        result = await self.agent.invoke_async(
            prompt,
            invocation_state={"user_key": "user_value", "non_json": object()},
        )
        return str(result)


async def test_invocation_state_round_trip(client: Client):
    _RECEIVED.clear()
    plugin = StrandsPlugin(models={"recording": lambda: _RecordingModel()})

    async with Worker(
        client,
        task_queue="test_invocation_state",
        workflows=[_InvocationStateWorkflow],
        plugins=[plugin],
        max_cached_workflows=0,
    ):
        await client.execute_workflow(
            _InvocationStateWorkflow.run,
            "hi",
            id=f"test_invocation_state_{uuid4()}",
            task_queue="test_invocation_state",
        )

    # The serializable key crosses the activity boundary; the non-serializable
    # one is dropped before dispatch (with a debug log).
    assert _RECEIVED, "model.stream() was not called"
    received = _RECEIVED[0]
    assert received.get("user_key") == "user_value"
    assert "non_json" not in received
