from __future__ import annotations

import json
from collections.abc import AsyncIterable
from typing import Any

from strands.models import Model
from strands.types.streaming import StreamEvent


class MockModel(Model):
    """Scripted Strands ``Model`` for tests.

    Each entry in ``responses`` is consumed by one ``stream()`` call. A ``str``
    yields a text turn; a ``dict`` of ``{name, input}`` yields a tool-use turn.
    """

    def __init__(self, responses: list[str | dict[str, Any]]) -> None:
        self._responses = list(responses)
        self._tool_call_index = 0

    def update_config(self, **_model_config: Any) -> None:
        return None

    def get_config(self) -> dict[str, Any]:
        return {}

    def structured_output(self, *_args: Any, **_kwargs: Any):
        raise NotImplementedError

    async def stream(self, *_args: Any, **_kwargs: Any) -> AsyncIterable[StreamEvent]:
        if not self._responses:
            raise AssertionError("MockModel script exhausted")
        response = self._responses.pop(0)

        yield {"messageStart": {"role": "assistant"}}

        if isinstance(response, str):
            yield {"contentBlockDelta": {"delta": {"text": response}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        else:
            self._tool_call_index += 1
            yield {
                "contentBlockStart": {
                    "start": {
                        "toolUse": {
                            "name": response["name"],
                            "toolUseId": f"mock-tool-{self._tool_call_index}",
                        },
                    },
                },
            }
            yield {
                "contentBlockDelta": {
                    "delta": {"toolUse": {"input": json.dumps(response["input"])}},
                },
            }
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
